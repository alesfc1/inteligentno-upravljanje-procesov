import csv
import io
import math
from datetime import datetime
from pathlib import Path

import streamlit as st
from pypdf import PdfReader

from data.db.database import (
    EVENT_COLUMNS,
    UPLOADS_DIR,
    create_database,
    get_cases,
    get_connection,
    get_events,
    insert_event,
    next_case_id
)


ROLE_IDENTITIES = {
    "stranka": ["stranka_001", "stranka_002", "stranka_003", "stranka_004", "stranka_005"],
    "referent": ["referent_01", "referent_02", "referent_03"],
    "vodja": ["vodja_01"],
}
PDF_FIELDS = ("znesek", "vrsta_skode", "stevilka_police")
CLAIM_TYPES = {"vozilo", "dom", "oprema", "poplava"}

STATUS_LABELS = {
    "oddano": "Oddano",
    "v_pregledu": "V pregledu",
    "caka_na_dopolnitev": "Čaka na dopolnitev",
    "predano_vodji": "Predano vodji",
    "odobren": "Odobren",
    "zavrnjen": "Zavrnjen",
    "umik_zahtevka": "Umik zahtevka",
    "umaknjen": "Umaknjen",
    "zakljucen_odobren": "Zaključen - odobren",
    "zakljucen_zavrnjen": "Zaključen - zavrnjen",
    "zakljucen_umaknjen": "Zaključen - umaknjen",
}


def now_iso() -> str:
    """Vrne trenutni čas z navedbo časovnega pasu."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def pdf_file_data(uploaded_file) -> tuple[str, bytes]:
    file_bytes = uploaded_file.getvalue()
    if not file_bytes.startswith(b"%PDF-"):
        raise ValueError("Naložena datoteka ni veljaven PDF dokument.")
    return Path(uploaded_file.name).name, file_bytes


def read_claim_form(uploaded_file) -> tuple[dict, list[str]]:
    """Prebere in preveri interaktivna polja PDF obrazca."""
    _, content = pdf_file_data(uploaded_file)
    try:
        fields = PdfReader(io.BytesIO(content)).get_fields() or {}
    except Exception as error:
        raise ValueError("PDF obrazca ni mogoče prebrati.") from error
    values = {name: fields.get(name, {}).get("/V") for name in PDF_FIELDS}
    errors = []
    if values["znesek"] in (None, ""):
        errors.append("Manjka znesek.")
    else:
        try:
            values["znesek"] = float(str(values["znesek"]).replace(",", "."))
            if not math.isfinite(values["znesek"]) or values["znesek"] <= 0:
                errors.append("Znesek mora biti večji od 0 EUR.")
        except (TypeError, ValueError):
            errors.append("Znesek ni veljavno število.")
    values["vrsta_skode"] = str(values["vrsta_skode"] or "").strip()
    if values["vrsta_skode"] not in CLAIM_TYPES:
        errors.append("Izberite dovoljeno vrsto škode.")
    values["stevilka_police"] = str(values["stevilka_police"] or "").strip()
    if not values["stevilka_police"]:
        errors.append("Manjka številka police.")
    return values, errors


def save_document(
    connection,
    *,
    case_id: str,
    uploaded_file,
    document_type: str,
    file_prefix: str,
) -> Path:
    safe_name, file_bytes = pdf_file_data(uploaded_file)
    destination = UPLOADS_DIR / f"{case_id}_{file_prefix}_{safe_name}"
    destination.write_bytes(file_bytes)
    connection.execute(
        """
        INSERT INTO documents (
            case_id, document_name, document_path, document_type, uploaded_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (case_id, safe_name, str(destination), document_type, now_iso()),
    )
    return destination


def create_case(
    *,
    applicant: str,
    uploaded_file,
) -> str:
    """Ustvari zahtevek iz PDF obrazca in samodejno preveri podatke."""
    safe_name, _ = pdf_file_data(uploaded_file)
    values, errors = read_claim_form(uploaded_file)
    amount = values["znesek"] if isinstance(values["znesek"], float) else 0.0
    title = f"Zahtevek – {values['vrsta_skode'] or 'neznana škoda'}"
    description = f"Polica: {values['stevilka_police'] or 'ni navedena'}"
    created_at = now_iso()
    validation_status = "v_pregledu" if not errors else "caka_na_dopolnitev"

    with get_connection() as connection:
        case_id = next_case_id(connection)
        destination = UPLOADS_DIR / f"{case_id}_osnovni_{safe_name}"

        try:
            connection.execute(
                """
                INSERT INTO cases (
                    case_id, title, description, znesek, applicant,
                    status, documents_complete, document_name, document_path,
                    created_at, vrsta_skode, stevilka_police
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    title.strip(),
                    description.strip(),
                    amount,
                    applicant,
                    validation_status,
                    not errors,
                    safe_name,
                    str(destination),
                    created_at,
                    values["vrsta_skode"],
                    values["stevilka_police"],
                ),
            )
            save_document(
                connection,
                case_id=case_id,
                uploaded_file=uploaded_file,
                document_type="osnovna dokumentacija",
                file_prefix="osnovni",
            )
            insert_event(
                connection,
                case_id=case_id,
                activity="Oddaja zahtevka",
                timestamp=created_at,
                resource=applicant,
                role="stranka",
                source="manual", 
                status="oddano",
                documents_complete=not errors,
                znesek=amount, vrsta_skode=values["vrsta_skode"],
                stevilka_police=values["stevilka_police"],
            )

            insert_event(
                connection,
                case_id=case_id,
                activity="Samodejno preverjanje",
                timestamp=now_iso(),
                resource="sistem",
                role="sistem",
                source="system",
                status=validation_status,
                documents_complete=not errors,
                znesek=amount, vrsta_skode=values["vrsta_skode"],
                stevilka_police=values["stevilka_police"],
            )
        except Exception:
            
            if destination is not None and destination.exists():
                destination.unlink()
            raise

    return case_id


def apply_action(
    case_id: str,
    action: str,
    resource: str,
    role: str,
    uploaded_file=None,
) -> None:
    """
    Preveri dovoljenja in stanje, izvede eno akcijo ter zabeleži dogodek.
    Opomba: seznam prehodov naj ostane usklajen s konceptualnim dnevnikom iz 3. naloge.
    """
    actions = {
        "take": ("Prevzem zahtevka", "v_pregledu", "referent"),
        "approve": ("Odobritev referenta", "zakljucen_odobren", "referent"),
        "reject": ("Zavrnitev referenta", "zakljucen_zavrnjen", "referent"),
        "request_completion": (
            "Zahteva za dopolnitev",
            "caka_na_dopolnitev",
            "referent",
        ),
        "handoff": ("Predaja vodji", "predano_vodji", "referent"),
        "manager_review": ("Pregled vodje", "v_pregledu", "vodja"),
        "manager_approve": (
            "Odobritev vodje",
            "zakljucen_odobren",
            "vodja",
        ),
        "withdraw": ("Potrditev umika", "zakljucen_umaknjen", "referent"),
        "submit_completion": ("Oddaja dopolnitve", "v_pregledu", "stranka"),
    }

    if action not in actions:
        raise ValueError("Neznana akcija.")

    activity, new_status, expected_role = actions[action]
    if role != expected_role:
        raise PermissionError("Za to akcijo izbrana vloga ni dovoljena.")

    with get_connection() as connection:
        case = connection.execute(
            "SELECT * FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if case is None:
            raise ValueError("Zahtevek ne obstaja.")

        allowed_statuses = {
            "take": {"v_pregledu"},
            "approve": {"v_pregledu"},
            "reject": {"v_pregledu"},
            "request_completion": {"v_pregledu"},
            "handoff": {"v_pregledu"},
            "manager_review": {"predano_vodji"},
            "manager_approve": {"v_pregledu"},
            "withdraw": {"v_pregledu"},
            "submit_completion": {"caka_na_dopolnitev"},
        }
        if case["status"] not in allowed_statuses[action]:
            raise ValueError("Akcija v trenutnem stanju ni dovoljena.")

        if action == "take" and case["assigned_to"] is not None:
            raise ValueError("Zahtevek je že prevzel drug referent.")

        if role == "vodja" and action == "manager_review":
            if case["status"] != "predano_vodji":
                raise PermissionError("Zahtevek ni predan vodji.")

        if role == "referent" and action not in {"take"}:
            if case["assigned_to"] != resource:
                raise PermissionError(
                    "Pred obdelavo mora referent prevzeti zahtevek."
                )

        if action == "submit_completion" and case["applicant"] != resource:
            raise PermissionError("Dopolnitev lahko odda samo stranka zahtevka.")

        if action == "submit_completion":
            if uploaded_file is None:
                raise ValueError("Naložite izpolnjen PDF obrazec dopolnitve.")
            values, errors = read_claim_form(uploaded_file)
            save_document(
                connection,
                case_id=case_id,
                uploaded_file=uploaded_file,
                document_type="dopolnitev dokumentacije",
                file_prefix="dopolnitev",
            )
            new_status = "v_pregledu" if not errors else "caka_na_dopolnitev"
            connection.execute(
                """UPDATE cases SET znesek=?, vrsta_skode=?,
                   stevilka_police=? WHERE case_id=?""",
                (values["znesek"] if isinstance(values["znesek"], float) else 0.0,
                 values["vrsta_skode"], values["stevilka_police"], case_id),
            )

        timestamp = now_iso()

        if action == "take":
            connection.execute(
                "UPDATE cases SET assigned_to = ? WHERE case_id = ?",
                (resource, case_id),
            )

        if action == "submit_completion":
            documents_complete = not errors
        elif action in {"approve", "manager_approve"}:
            documents_complete = True
        elif action == "request_completion":
            documents_complete = False
        else:
            documents_complete = (
                None if case["documents_complete"] is None
                else bool(case["documents_complete"])
            )
        connection.execute(
            """
            UPDATE cases
            SET status = ?, documents_complete = ?
            WHERE case_id = ?
            """,
            (new_status, documents_complete, case_id),
        )

        # SPREMEMBA: človeške akcije so manual, sistemske pa system.
        event_source = "system" if role == "sistem" else "manual"
        insert_event(
            connection,
            case_id=case_id,
            activity=activity,
            timestamp=timestamp,
            resource=resource,
            role=role,
            source=event_source,
            status=new_status,
            documents_complete=documents_complete,
            znesek=(values["znesek"] if action == "submit_completion" else case["znesek"]),
            vrsta_skode=(values["vrsta_skode"] if action == "submit_completion" else case["vrsta_skode"]),
            stevilka_police=(values["stevilka_police"] if action == "submit_completion" else case["stevilka_police"]),
        )

        if action in {"approve", "manager_approve", "reject", "withdraw"}:
            insert_event(
                connection,
                case_id=case_id,
                activity="Obvestilo stranki",
                timestamp=now_iso(),
                resource="sistem",
                role="sistem",
                source="system",
                status=new_status,
                znesek=case["znesek"],
                documents_complete=documents_complete,
                vrsta_skode=case["vrsta_skode"],
                stevilka_police=case["stevilka_police"],
            )


def events_csv() -> str:
    """Izvozi celoten dnevnik, urejen po case_id in timestamp."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=EVENT_COLUMNS)
    writer.writeheader()
    # SPREMEMBA: get_events() že vrača dogodke po case_id, timestamp in id.
    for event in get_events():
        writer.writerow({column: event[column] for column in EVENT_COLUMNS})
    return output.getvalue()


def render_submission(identity: str) -> None:
    st.subheader("Oddaja zahtevka")
    st.caption("Izpolnite polja v PDF obrazcu in ga naložite. Podatkov ni treba prepisati.")
    template = Path(__file__).parent / "data" / "forms" / "obrazec_zahtevka.pdf"
    if template.exists():
        st.download_button("Prenesi PDF obrazec", template.read_bytes(),
                           file_name=template.name, mime="application/pdf")
    with st.form("new_case_form", clear_on_submit=True):
        document = st.file_uploader("Izpolnjen PDF obrazec", type=["pdf"])
        submitted = st.form_submit_button("Oddaj zahtevek")

    if submitted:
        if document is None:
            st.error("Priložite PDF dokument.")
        else:
            try:
                case_id = create_case(
                    applicant=identity,
                    uploaded_file=document,
                )
                case = next(c for c in get_cases() if c["case_id"] == case_id)
                if case["status"] == "caka_na_dopolnitev":
                    st.warning(f"Zahtevek {case_id} čaka na dopolnitev. Preverite obvezna polja obrazca.")
                else:
                    st.success(f"Zahtevek {case_id} je oddan in samodejno preverjen.")
                st.rerun()
            except (ValueError, OSError) as error:
                st.error(f"Zahtevka ni bilo mogoče oddati: {error}")


def render_cases(identity: str, role: str) -> None:
    st.subheader("Zahtevki")
    cases = get_cases()

    visible_cases = (
        [case for case in cases if case["applicant"] == identity]
        if role == "stranka"
        else cases
    )
    if not visible_cases:
        st.info("Ni zahtevkov za prikaz.")
        return

    for case in visible_cases:
        with st.container(border=True):
            st.markdown(
                f"**{case['case_id']} - {case['title']}**  \n"
                f"Stanje: **{status_label(case['status'])}** | "
                f"Znesek: **{case['znesek'] or 0:.2f} EUR** | "
                f"Vrsta: **{case['vrsta_skode'] or '—'}** | "
                f"Polica: **{case['stevilka_police'] or '—'}**"
            )
            assigned = case["assigned_to"] or "ni prevzet"
            st.caption(
                f"Stranka: {case['applicant']} | Referent: {assigned} | "
                f"Dokument: {case['document_name']}"
            )

            if role == "stranka" and case["status"] == "caka_na_dopolnitev":
                completion_document = st.file_uploader(
                    "PDF dopolnitve",
                    type=["pdf"],
                    key=f"completion_file_{case['case_id']}",
                )
                if st.button("Oddaj dopolnitev", key=f"complete_{case['case_id']}"):
                    try:
                        apply_action(
                            case["case_id"],
                            "submit_completion",
                            identity,
                            role,
                            completion_document,
                        )
                        st.success("Dopolnitev je sprejeta." if case["documents_complete"] else "Obrazec še ni popoln; zahtevek ostaja v dopolnitvi.")
                        st.rerun()
                    except (ValueError, PermissionError) as error:
                        st.error(str(error))

            if (
                role == "referent"
                and case["status"] == "v_pregledu"
                and case["assigned_to"] is None
            ):
                if st.button(
                    "Prevzemi zahtevek",
                    key=f"take_{case['case_id']}",
                ):
                    try:
                        apply_action(case["case_id"], "take", identity, role)
                        st.rerun()
                    except (ValueError, PermissionError) as error:
                        st.error(str(error))

            elif (
                role == "referent"
                and case["status"] == "v_pregledu"
                and case["assigned_to"] == identity
            ):
                buttons = [
                    ("Odobri", "approve"),
                    ("Zavrni", "reject"),
                    ("Zahtevaj dopolnitev", "request_completion"),
                    ("Predaj vodji", "handoff"),
                    ("Potrdi umik", "withdraw"),
                ]
                cols = st.columns(len(buttons))
                for column, (label, action) in zip(cols, buttons):
                    if column.button(label, key=f"{action}_{case['case_id']}"):
                        try:
                            apply_action(case["case_id"], action, identity, role)
                            st.rerun()
                        except (ValueError, PermissionError) as error:
                            st.error(str(error))

            elif role == "vodja" and case["status"] == "predano_vodji":
                if st.button(
                    "Preglej zahtevek",
                    key=f"manager_review_{case['case_id']}",
                ):
                    try:
                        apply_action(
                            case["case_id"], "manager_review", identity, role
                        )
                        st.rerun()
                    except (ValueError, PermissionError) as error:
                        st.error(str(error))
            elif role == "vodja" and case["status"] == "v_pregledu":
                if st.button(
                    "Odobri",
                    key=f"manager_approve_{case['case_id']}",
                ):
                    try:
                        apply_action(
                            case["case_id"], "manager_approve", identity, role
                        )
                        st.rerun()
                    except (ValueError, PermissionError) as error:
                        st.error(str(error))


def render_event_log() -> None:
    st.subheader("Dnevnik dogodkov")
    events = get_events()
    if events:
        st.dataframe(
            [
                {column: event[column] for column in EVENT_COLUMNS}
                for event in events
            ],
            use_container_width=True,
            hide_index=True,
        )
    st.download_button(
        "Izvozi dnevnik v CSV",
        data=events_csv(),
        file_name="dnevnik.csv",
        mime="text/csv",
    )


def main() -> None:
    st.set_page_config(
        page_title="Obravnava zavarovalniškega zahtevka",
        layout="wide",
    )
    create_database()
    st.title("Obravnava zavarovalniškega zahtevka")

    with st.sidebar:
        st.header("Izbira uporabnika")
        role = st.selectbox("Vloga", list(ROLE_IDENTITIES))
        identity = st.selectbox("Identiteta", ROLE_IDENTITIES[role])
        st.divider()
        st.write(f"Prijavljen uporabnik: **{identity}**")
        st.write(f"Vloga: **{role}**")

    if role == "stranka":
        render_submission(identity)

    render_cases(identity, role)

    render_event_log()


if __name__ == "__main__":
    main()
