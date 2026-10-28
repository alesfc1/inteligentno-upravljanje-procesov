# Inteligentno Upravljanje Procesov

## Opis aplikacije

Aplikacija, razvita v Pythonu z uporabo ogrodja Streamlit, omogoča digitalizirano obravnavo zavarovalniških zahtevkov. Podpira različne uporabniške vloge ter spremljanje poteka zahtevkov skozi posamezne faze procesa.

## Zagon aplikacije

V korenski mapi projekta namestite odvisnosti in zaženite Streamlit:

```powershell
py -3 -m pip install -r requirements.txt
py -3 -m streamlit run app.py
```

Aplikacija ob prvem zagonu ustvari SQLite podatkovno bazo:
`data/db/iup.db`. 

Naloženi PDF dokumenti se shranijo v `data/uploads/`
