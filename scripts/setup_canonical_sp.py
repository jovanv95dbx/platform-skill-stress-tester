#!/usr/bin/env python3
"""One-shot setup: create the canonical Account-Admin service principal for stress-test runs.

This is the AWS-side prereq. The framework's AWS auth pattern relies on a
single Account-Admin SP whose env-var creds authenticate BOTH the account-
level (mws) and workspace-level (created_workspace) Terraform providers.
With this SP, no per-workspace browser login or PAT is needed.

What this script does (idempotent within a run):
  1. Connects to your Databricks AWS account as an existing account-U2M user
     (browser login already done — you provide the profile name).
  2. Creates a service principal named <display-name>.
  3. Grants it the `account_admin` SCIM role via PATCH.
     (NOTE: the `update_rule_set` API does NOT accept `roles/account.admin`;
     SCIM PATCH on the SP's `roles` field is the correct path.)
  4. Mints an OAuth client secret.
  5. Writes credentials to /tmp/canonical-sp/creds.json (mode 600) and
     a shell-sourceable /tmp/canonical-sp/sourceme.

Usage:
    python3 scripts/setup_canonical_sp.py --profile aws-account-u2m
    python3 scripts/setup_canonical_sp.py --profile aws-account-u2m --display-name my-run-deployer
    python3 scripts/setup_canonical_sp.py --profile aws-account-u2m --out-dir ~/.cache/canonical-sp

Then, before any terraform run:
    source /tmp/canonical-sp/sourceme

Or per-command:
    env $(cat /tmp/canonical-sp/sourceme | sed 's/^export //' | tr '\\n' ' ') terraform apply

Notes:
  - The OAuth secret is shown ONLY at creation time. Capture it from the
    creds.json the script writes.
  - Both the SP and the secret persist server-side until you delete them.
    Re-running the script creates ANOTHER SP — it does not update existing.
    For a one-account-one-SP setup, delete the old SP from the account
    console first, OR pass --display-name with a fresh value.
  - This script targets AWS Databricks (https://accounts.cloud.databricks.com).
    For Azure, the canonical pattern uses azure-cli auth — no equivalent SP
    creation is needed; just `az login` and set `auth_type = "azure-cli"`
    in your Terraform databricks provider.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--profile",
        required=True,
        help="Existing account-U2M profile name in ~/.databrickscfg "
             "(must point at https://accounts.cloud.databricks.com with auth_type=databricks-cli)",
    )
    p.add_argument(
        "--display-name",
        default="stress-tester-deployer",
        help="SP display name (default: stress-tester-deployer)",
    )
    p.add_argument(
        "--out-dir",
        default="/tmp/canonical-sp",
        help="Output dir for creds.json + sourceme (default: /tmp/canonical-sp)",
    )
    p.add_argument(
        "--no-account-admin",
        action="store_true",
        help="Skip granting account_admin (useful for testing the SDK calls; the SP will not be able to deploy without it)",
    )
    args = p.parse_args()

    try:
        from databricks.sdk import AccountClient
        from databricks.sdk.service.iam import Patch, PatchOp, PatchSchema
    except ImportError:
        print("ERROR: databricks-sdk not installed. Run: pip install databricks-sdk", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Using account profile: {args.profile}")
    try:
        ac = AccountClient(profile=args.profile)
    except Exception as e:
        print(f"ERROR: could not load profile '{args.profile}': {e}", file=sys.stderr)
        print("Hint: make sure ~/.databrickscfg has a profile pointing at "
              "https://accounts.cloud.databricks.com with auth_type=databricks-cli "
              "(run `databricks auth login --host https://accounts.cloud.databricks.com --account-id <id>` first)",
              file=sys.stderr)
        return 1

    print(f"Account ID: {ac.config.account_id}")

    # 1. Create the SP
    print(f"\nCreating service principal: {args.display_name} ...")
    try:
        sp = ac.service_principals.create(display_name=args.display_name)
    except Exception as e:
        print(f"ERROR creating SP: {e}", file=sys.stderr)
        return 1
    print(f"  ✅ SP id={sp.id}  application_id={sp.application_id}")

    # 2. Grant account_admin
    if not args.no_account_admin:
        print("\nGranting account_admin role via SCIM PATCH ...")
        try:
            ac.service_principals.patch(
                id=str(sp.id),
                schemas=[PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
                operations=[Patch(
                    op=PatchOp.ADD,
                    path="roles",
                    value=[{"value": "account_admin"}],
                )],
            )
        except Exception as e:
            print(f"ERROR granting account_admin: {e}", file=sys.stderr)
            print("  → the SP exists but is not Account Admin. Delete it via the account console "
                  "(User management → Service principals) and retry, or grant the role via the UI.",
                  file=sys.stderr)
            return 2
        # Verify
        sp_after = ac.service_principals.get(id=str(sp.id))
        roles = [r.value for r in (sp_after.roles or [])]
        if "account_admin" not in roles:
            print(f"WARN: account_admin not visible in SP roles after PATCH (got {roles}). "
                  "Continuing, but auth may fail.", file=sys.stderr)
        else:
            print(f"  ✅ account_admin granted (verified)")

    # 3. Mint OAuth secret
    print("\nMinting OAuth secret ...")
    try:
        sec = ac.service_principal_secrets.create(service_principal_id=str(sp.id))
    except Exception as e:
        print(f"ERROR minting secret: {e}", file=sys.stderr)
        return 3
    print(f"  ✅ secret minted (will not be shown again — saving to disk)")

    # 4. Write creds.json (mode 600)
    creds_path = out_dir / "creds.json"
    creds_path.write_text(json.dumps({
        "client_id": sp.application_id,
        "client_secret": sec.secret,
        "sp_id": str(sp.id),
        "sp_display_name": args.display_name,
        "databricks_account_id": ac.config.account_id,
        "host": "https://accounts.cloud.databricks.com",
    }, indent=2), encoding="utf-8")
    creds_path.chmod(0o600)
    print(f"\n  📝 wrote {creds_path}")

    # 5. Write sourceme (shell-sourceable export of env vars)
    sourceme_path = out_dir / "sourceme"
    sourceme_path.write_text(
        f'export DATABRICKS_CLIENT_ID={sp.application_id}\n'
        f'export DATABRICKS_CLIENT_SECRET={sec.secret}\n'
        f'export DATABRICKS_ACCOUNT_ID={ac.config.account_id}\n',
        encoding="utf-8",
    )
    sourceme_path.chmod(0o600)
    print(f"  📝 wrote {sourceme_path}")

    print("\n✅ Canonical SP setup complete.\n")
    print("Next steps:")
    print(f"  1. source {sourceme_path}")
    print("  2. Verify: databricks current-user me \\")
    print("       --host https://<your-workspace>.cloud.databricks.com (after first deploy)")
    print("  3. For Terraform: prefix every apply with `env -u DATABRICKS_HOST` to avoid stale-host overrides:")
    print("       env -u DATABRICKS_HOST terraform apply")
    print()
    print("Reminder: creds at", creds_path, "are SENSITIVE. Don't commit them, don't share them.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
