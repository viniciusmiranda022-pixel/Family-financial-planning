"""Local break-glass MFA reset (docs/WORK_ORDER_LOCAL_MFA_TOTP.md, "Break-glass
por acesso local ao servidor").

Only ever runs with direct access to the host/database -- never exposed as
an HTTP route, and intentionally has no "master password", universal code
or hardcoded recovery path. It removes/inutilizes the user's TOTP factor
and every recovery code, revokes every previously issued session
(`session_version`), and leaves the account in mandatory-enrollment state
on its next login -- exactly the same state a brand-new user is in, never
a bypass of MFA itself.

    python -m app.cli.mfa reset --username <usuario>

Requires an explicit `--yes` confirmation (or an interactive "sim" prompt)
so a mistyped username cannot silently reset the wrong account; never
prints the previous secret, and the account itself is left untouched
(active, password unchanged) -- only its MFA factor is invalidated.
"""

import argparse
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import audit
from app.db import SessionLocal
from app.models import MfaFactor, User
from app.security import bump_session_version


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reset local de emergência do segundo fator (MFA) de um usuário -- "
            "somente com acesso direto ao host/banco. Nunca exposto como rota web."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    reset_parser = subparsers.add_parser(
        "reset", help="Remove o fator TOTP e os recovery codes de um usuário e revoga sessões."
    )
    reset_parser.add_argument("--username", required=True, help="Username exato do usuário afetado.")
    reset_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirma o reset sem prompt interativo (uso em automação local/rescue).",
    )

    return parser.parse_args(argv)


def _confirm(username: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    answer = input(
        f"Confirma o reset local de MFA para o usuário '{username}'? "
        "O fator atual e todos os recovery codes serão invalidados e o "
        "próximo login exigirá novo enrollment. Digite 'sim' para confirmar: "
    )
    return answer.strip().lower() == "sim"


def reset_mfa_for_user(db: Session, username: str) -> bool:
    """Core reset logic, separated from the CLI/`SessionLocal` wrapper so
    it can run against any `Session` -- production's real database via
    `main()`, or a test's isolated one directly (`app.db.engine` is a
    process-wide singleton bound at import time, same reasoning as every
    other CLI's `run_*(db, ...)`/`main()` split in this codebase, e.g.
    `app.cli.backfill.run_backfill`).

    Returns whether a reset actually happened. `False` (no row touched)
    for a nonexistent or inactive user -- "reset de usuário inexistente/
    inativo falha explicitamente sem alterar outro usuário."
    """

    user = db.scalar(select(User).where(User.username == username))
    if user is None or not user.active:
        return False

    factor = db.scalar(select(MfaFactor).where(MfaFactor.user_id == user.id))
    if factor is not None:
        # `MfaFactor.recovery_codes` is `cascade="all, delete-orphan"` (and
        # the FK itself is `ondelete="CASCADE"`), so deleting the factor
        # row also removes every recovery code -- no separate delete
        # statement needed.
        db.delete(factor)
    bump_session_version(db, user)
    audit(db, user, "mfa.reset_local", entity_type="user", entity_id=user.id, source="cli")
    db.commit()
    return True


def reset_mfa(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    if args.command != "reset":
        print("Comando desconhecido.", file=sys.stderr)
        return 2

    username = args.username.strip().lower()
    if not _confirm(username, assume_yes=args.yes):
        print("Operação cancelada.")
        return 1

    with SessionLocal() as db:
        if not reset_mfa_for_user(db, username):
            print(f"Usuário '{username}' não encontrado ou inativo. Nenhuma alteração feita.", file=sys.stderr)
            return 1

    print(
        f"MFA resetado para '{username}'. Sessões anteriores revogadas; "
        "próximo login exigirá novo enrollment."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    return reset_mfa(argv)


if __name__ == "__main__":
    raise SystemExit(main())
