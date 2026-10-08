"""Run locally as Lab Gmail; store output only in private deployment state."""

import argparse
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-secrets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Refusing to overwrite existing OAuth token")
    flow = InstalledAppFlow.from_client_secrets_file(
        str(args.client_secrets), scopes=["https://www.googleapis.com/auth/drive"])
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(credentials.to_json(), encoding="utf-8")
    print(f"Lab Gmail Drive token saved: {args.output}")


if __name__ == "__main__":
    main()
