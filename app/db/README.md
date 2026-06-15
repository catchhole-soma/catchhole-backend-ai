# db

Database engine/session setup lives here.

Expected future files:

- `session.py`: SQLAlchemy engine and session factory
- `transaction.py`: transaction helpers, if needed

This package is infrastructure-level code. Domain logic should stay in services, worker, or analysis packages.
