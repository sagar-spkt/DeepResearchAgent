"""Explicit database migration and bootstrap commands."""

import argparse

from sqlalchemy import select

from .config import Settings
from .db import create_session_factory, migrate
from .models import User


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    owner = commands.add_parser("bootstrap-owner")
    owner.add_argument(
        "--subject", required=True, help="Verified Cognito user subject, not an email header"
    )
    owner.add_argument("--email", required=True)
    args = parser.parse_args()
    settings = Settings()
    factory = create_session_factory(settings)
    if args.command == "migrate":
        migrate(factory)
        from .research import initialize_checkpoints

        initialize_checkpoints(settings)
        print("Database schema is at version 1")
    elif args.command == "bootstrap-owner":
        with factory.begin() as session:
            from .api import serialize

            serialize(session)
            if session.scalar(select(User.id).where(User.role == "owner")):
                raise SystemExit("Owner already exists; use the owner-authorized admin API")
            user = session.get(User, args.subject)
            if user is None:
                user = User(id=args.subject, email=args.email)
                session.add(user)
            user.role = "owner"
            user.approved = True
        print("Owner bootstrapped")


if __name__ == "__main__":
    main()
