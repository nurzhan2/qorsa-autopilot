"""Управление хранилищем секретов.

    python scripts/vault_cli.py genkey          # сгенерировать VAULT_KEY для .env
    python scripts/vault_cli.py list            # только имена, значений не показывает
    python scripts/vault_cli.py add FTP_PASS    # значение спросит, не отображая
    python scripts/vault_cli.py add FTP_PASS --stdin < file
    python scripts/vault_cli.py rm FTP_PASS

Значения не печатаются ни одной командой: показать секрет — это записать его
в историю терминала и в скроллбек. Нужен он агенту, а не глазам.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cryptography.fernet import Fernet          # noqa: E402

from autopilot.config import cfg                # noqa: E402
from autopilot.vault import Vault, ref          # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="хранилище секретов Qorsa Autopilot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("genkey", help="сгенерировать ключ Fernet")
    sub.add_parser("list", help="показать имена секретов")
    p_add = sub.add_parser("add", help="добавить или заменить секрет")
    p_add.add_argument("name")
    p_add.add_argument("--stdin", action="store_true", help="взять значение со stdin")
    p_rm = sub.add_parser("rm", help="удалить секрет")
    p_rm.add_argument("name")
    args = ap.parse_args()

    if args.cmd == "genkey":
        print(Fernet.generate_key().decode())
        print("\nположи это в .env как VAULT_KEY=... и никому не показывай", file=sys.stderr)
        return 0

    vault = Vault()
    if not vault.enabled:
        print("VAULT_KEY не задан. Сначала: python scripts/vault_cli.py genkey", file=sys.stderr)
        return 2

    if args.cmd == "list":
        names = vault.names()
        if not names:
            print(f"хранилище пустое ({cfg.vault_path})")
            return 0
        print(f"{cfg.vault_path} — {len(names)} шт.:")
        for n in names:
            print(f"  {n:24s} {ref(n)}")
        return 0

    if args.cmd == "add":
        if args.stdin:
            value = sys.stdin.read().strip()
        else:
            value = getpass.getpass(f"значение для {args.name} (не отображается): ")
        if not value:
            print("пустое значение, ничего не сохранил", file=sys.stderr)
            return 2
        vault.set(args.name, value)
        print(f"сохранил. В БД и в задачах ссылайся так: {ref(args.name)}")
        return 0

    if args.cmd == "rm":
        print("удалил" if vault.delete(args.name) else "такого секрета нет")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
