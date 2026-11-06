# KPCL POC

KPCL Generation Management System (GMS) data warehouse, with an NL2SQL AI app and a voice app on top.
The full handoff is in **[HANDOFF.md](HANDOFF.md)**. The architecture is in [NL2SQL_LAYERED_ARCHITECTURE.md](NL2SQL_LAYERED_ARCHITECTURE.md).

## Get the database first (it is not in git)

The warehouse `warehouse/kpcl_gms.db` (443 MB) is too large for a git repo.
Download `kpcl_gms.db.zip` from this repo's **Releases** page (release `data`) and unzip it into `warehouse/`.
`webapp/integrity_manifest.json` checks the file's SHA-256, so use the released file as-is.

## Run

```bat
copy webapp\.env.example webapp\.env      :: fill in the gateway keys
cd webapp\kpcl-ai-app
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

To run the voice app, use `webapp\kpcl-voice-app` on port 8001 (see HANDOFF.md).

## Not in this repo
- `.env` files: use `webapp/.env.example`.
- `warehouse/kpcl_gms.db`: download it from Releases.
- Server logs and `webapp/_backup_*`.

> Private: client data.
