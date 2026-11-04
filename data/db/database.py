import sqlite3
from pathlib import Path

DATABASE_DIR = Path(__file__).resolve().parent
DB_PATH = DATABASE_DIR / "iup.db"
UPLOADS_DIR = DATABASE_DIR.parent / "uploads"

EVENT_COLUMNS = (
    "case_id",
    "activity",
    "timestamp",
    "resource",
    "role",
    "source",
    "status",
    "documents_complete",
    "znesek",
    "vrsta_skode",
    "stevilka_police",
)


def get_connection() -> sqlite3.Connection:
    DATABASE_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def create_database() -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS cases (
                case_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                znesek REAL NOT NULL CHECK (znesek >= 0),
                applicant TEXT NOT NULL,
                status TEXT NOT NULL,
                assigned_to TEXT,
                documents_complete INTEGER,
                document_name TEXT NOT NULL,
                document_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL REFERENCES cases(case_id),
                activity TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                resource TEXT NOT NULL,
                role TEXT NOT NULL,
                source TEXT NOT NULL CHECK (source IN ('system', 'manual')),
                status TEXT NOT NULL,
                documents_complete INTEGER,
                znesek REAL,
                vrsta_skode TEXT,
                stevilka_police TEXT,
                UNIQUE (case_id, activity, timestamp)
            );
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL REFERENCES cases(case_id),
                document_name TEXT NOT NULL,
                document_path TEXT NOT NULL,
                document_type TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_case_timestamp
                ON events (case_id, timestamp);
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(cases)").fetchall()
        }
        if "assigned_to" not in columns:
            connection.execute("ALTER TABLE cases ADD COLUMN assigned_to TEXT")
        for column, declaration in {
            "znesek": "REAL NOT NULL DEFAULT 0",
            "vrsta_skode": "TEXT",
            "stevilka_police": "TEXT",
        }.items():
            if column not in columns:
                connection.execute(f"ALTER TABLE cases ADD COLUMN {column} {declaration}")
        if "claim_amount_eur" in columns:
            connection.execute(
                "UPDATE cases SET znesek = claim_amount_eur WHERE znesek IS NULL OR znesek = 0"
            )
            try:
                connection.execute("ALTER TABLE cases DROP COLUMN claim_amount_eur")
            except sqlite3.OperationalError:
                pass
        event_columns = {row["name"] for row in connection.execute("PRAGMA table_info(events)").fetchall()}
        for column, declaration in {
            "vrsta_skode": "TEXT",
            "stevilka_police": "TEXT",
        }.items():
            if column not in event_columns:
                connection.execute(f"ALTER TABLE events ADD COLUMN {column} {declaration}")
        if "znesek" not in event_columns:
            connection.execute("ALTER TABLE events ADD COLUMN znesek REAL")
        if "claim_amount_eur" in event_columns:
            connection.execute(
                "UPDATE events SET znesek = claim_amount_eur WHERE znesek IS NULL OR znesek = 0"
            )
            try:
                connection.execute("ALTER TABLE events DROP COLUMN claim_amount_eur")
            except sqlite3.OperationalError:
                pass
        connection.execute(
            """UPDATE events
               SET vrsta_skode = COALESCE(vrsta_skode, (
                       SELECT cases.vrsta_skode FROM cases
                       WHERE cases.case_id = events.case_id)),
                   stevilka_police = COALESCE(stevilka_police, (
                       SELECT cases.stevilka_police FROM cases
                       WHERE cases.case_id = events.case_id))
               WHERE activity = 'Obvestilo stranki'
                 AND (vrsta_skode IS NULL OR stevilka_police IS NULL)"""
        )
        connection.execute(
            """UPDATE cases
               SET vrsta_skode = COALESCE(vrsta_skode, (
                       SELECT events.vrsta_skode FROM events
                       WHERE events.case_id = cases.case_id
                         AND events.activity = 'Oddaja zahtevka'
                       ORDER BY events.timestamp, events.id LIMIT 1)),
                   stevilka_police = COALESCE(stevilka_police, (
                       SELECT events.stevilka_police FROM events
                       WHERE events.case_id = cases.case_id
                         AND events.activity = 'Oddaja zahtevka'
                       ORDER BY events.timestamp, events.id LIMIT 1))
               WHERE vrsta_skode IS NULL OR stevilka_police IS NULL"""
        )


def next_case_id(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT MAX(CAST(SUBSTR(case_id, 3) AS INTEGER)) AS number "
        "FROM cases WHERE case_id LIKE 'Z-%'"
    ).fetchone()
    next_number = (row["number"] or 0) + 1
    return f"Z-{next_number:03d}"


def insert_event(
    connection: sqlite3.Connection,
    *,
    case_id: str,
    activity: str,
    timestamp: str,
    resource: str,
    role: str,
    source: str,
    status: str,
    znesek: float | None,
    documents_complete: bool | None,
    vrsta_skode: str | None = None,
    stevilka_police: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO events (
            case_id, activity, timestamp, resource, role, source, status,
            znesek, documents_complete, vrsta_skode,
            stevilka_police
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            case_id,
            activity,
            timestamp,
            resource,
            role,
            source,
            status,
            znesek,
            None if documents_complete is None else int(documents_complete),
            vrsta_skode, stevilka_police,
        ),
    )


def get_cases() -> list[sqlite3.Row]:
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM cases ORDER BY created_at DESC"
        ).fetchall()


def get_events() -> list[sqlite3.Row]:
    with get_connection() as connection:
        return connection.execute(
            "SELECT " + ", ".join(EVENT_COLUMNS) +
            " FROM events ORDER BY case_id, timestamp, id"
        ).fetchall()


def get_case(case_id: str) -> sqlite3.Row | None:
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()


if __name__ == "__main__":
    create_database()
