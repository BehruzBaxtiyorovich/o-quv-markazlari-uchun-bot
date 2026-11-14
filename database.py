from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite

from config import DATABASE_PATH


@asynccontextmanager
async def _connect() -> AsyncIterator[aiosqlite.Connection]:
    db = await aiosqlite.connect(DATABASE_PATH)
    try:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        yield db
    finally:
        await db.close()


async def init_db() -> None:
    async with _connect() as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS centers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                card_number TEXT NOT NULL DEFAULT '',
                card_holder TEXT NOT NULL DEFAULT '',
                admin_telegram INTEGER NOT NULL,
                admin_username TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS staff (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                center_id INTEGER NOT NULL REFERENCES centers(id) ON DELETE CASCADE,
                telegram_id INTEGER NOT NULL,
                full_name TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL CHECK (role IN ('admin', 'teacher')),
                UNIQUE(center_id, telegram_id)
            );
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                center_id INTEGER NOT NULL REFERENCES centers(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                UNIQUE(center_id, name)
            );
            CREATE TABLE IF NOT EXISTS group_staff (
                group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                telegram_id INTEGER NOT NULL,
                PRIMARY KEY(group_id, telegram_id)
            );
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                full_name TEXT NOT NULL,
                parent_telegram_id INTEGER NOT NULL,
                parent_username TEXT NOT NULL DEFAULT '',
                balance_date TEXT NOT NULL,
                UNIQUE(group_id, full_name, parent_telegram_id)
            );
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                date TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('Keldi', 'Kelmadi')),
                UNIQUE(student_id, date)
            );
            """
        )
        columns = await (await db.execute("PRAGMA table_info(centers)")).fetchall()
        if "admin_username" not in {column["name"] for column in columns}:
            await db.execute(
                "ALTER TABLE centers ADD COLUMN admin_username TEXT NOT NULL DEFAULT ''"
            )
        staff_columns = await (await db.execute("PRAGMA table_info(staff)")).fetchall()
        if "full_name" not in {column["name"] for column in staff_columns}:
            await db.execute(
                "ALTER TABLE staff ADD COLUMN full_name TEXT NOT NULL DEFAULT ''"
            )
        if "username" not in {column["name"] for column in staff_columns}:
            await db.execute(
                "ALTER TABLE staff ADD COLUMN username TEXT NOT NULL DEFAULT ''"
            )
        student_columns = await (await db.execute("PRAGMA table_info(students)")).fetchall()
        if "parent_username" not in {column["name"] for column in student_columns}:
            await db.execute(
                "ALTER TABLE students ADD COLUMN parent_username TEXT NOT NULL DEFAULT ''"
            )
        scoped_access = await db.execute(
            "SELECT 1 FROM app_meta WHERE key = 'group_staff_scoped_v1'"
        )
        if not await scoped_access.fetchone():
            # Earlier versions granted each teacher access to every group automatically.
            await db.execute("DELETE FROM group_staff")
            await db.execute(
                "INSERT INTO app_meta(key, value) VALUES ('group_staff_scoped_v1', '1')"
            )
        await db.commit()


async def payment_reminders_run_date() -> str | None:
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT value FROM app_meta WHERE key = 'payment_reminders_run_date'"
        )
        row = await cursor.fetchone()
        return row["value"] if row else None


async def mark_payment_reminders_run(run_date: str) -> None:
    async with _connect() as db:
        await db.execute(
            """INSERT INTO app_meta(key, value)
               VALUES ('payment_reminders_run_date', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (run_date,),
        )
        await db.commit()


async def create_center(
    name: str, admin_id: int, admin_full_name: str, admin_username: str = ""
) -> int:
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT INTO centers(name, admin_telegram, admin_username) VALUES (?, ?, ?)",
            (name, admin_id, admin_username),
        )
        center_id = cursor.lastrowid
        await db.execute(
            """INSERT INTO staff(center_id, telegram_id, full_name, username, role)
               VALUES (?, ?, ?, ?, 'admin')""",
            (center_id, admin_id, admin_full_name, admin_username),
        )
        await db.commit()
        return int(center_id)


async def update_center_payment_details(
    center_id: int, card_number: str, card_holder: str, admin_username: str
) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE centers
               SET card_number = ?, card_holder = ?, admin_username = ?
               WHERE id = ?""",
            (card_number, card_holder, admin_username, center_id),
        )
        await db.commit()


async def list_centers() -> list[aiosqlite.Row]:
    async with _connect() as db:
        cursor = await db.execute("SELECT * FROM centers ORDER BY id")
        return await cursor.fetchall()


async def list_centers_page(limit: int, offset: int) -> list[aiosqlite.Row]:
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT c.*,
                      (SELECT COUNT(*) FROM groups g WHERE g.center_id = c.id)
                          AS group_count,
                      (SELECT COUNT(*) FROM students s
                       JOIN groups g ON g.id = s.group_id
                       WHERE g.center_id = c.id) AS student_count
               FROM centers c
               ORDER BY c.id LIMIT ? OFFSET ?""",
            (limit, offset),
        )
        return await cursor.fetchall()


async def list_groups_page(
    center_id: int, limit: int, offset: int
) -> list[aiosqlite.Row]:
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT * FROM groups WHERE center_id = ? ORDER BY name LIMIT ? OFFSET ?",
            (center_id, limit, offset),
        )
        return await cursor.fetchall()


async def list_students_page(
    group_id: int, limit: int, offset: int
) -> list[aiosqlite.Row]:
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT * FROM students WHERE group_id = ?
               ORDER BY full_name LIMIT ? OFFSET ?""",
            (group_id, limit, offset),
        )
        return await cursor.fetchall()


async def list_attendance_page(
    student_id: int, limit: int, offset: int
) -> list[aiosqlite.Row]:
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT date, status FROM attendance WHERE student_id = ?
               ORDER BY date DESC LIMIT ? OFFSET ?""",
            (student_id, limit, offset),
        )
        return await cursor.fetchall()


_MANAGED_TABLES = {
    "centers": {
        "pk": ("id",),
        "add": ("name", "card_number", "card_holder", "admin_telegram", "admin_username", "is_active"),
        "edit": ("name", "card_number", "card_holder", "admin_telegram", "admin_username", "is_active"),
        "int_fields": {"admin_telegram", "is_active"},
    },
    "staff": {
        "pk": ("id",),
        "add": ("center_id", "telegram_id", "full_name", "username", "role"),
        "edit": ("center_id", "telegram_id", "full_name", "username", "role"),
        "int_fields": {"center_id", "telegram_id"},
    },
    "groups": {
        "pk": ("id",),
        "add": ("center_id", "name"),
        "edit": ("center_id", "name"),
        "int_fields": {"center_id"},
    },
    "group_staff": {
        "pk": ("group_id", "telegram_id"),
        "add": ("group_id", "telegram_id"),
        "edit": (),
        "int_fields": {"group_id", "telegram_id"},
    },
    "students": {
        "pk": ("id",),
        "add": ("group_id", "full_name", "parent_telegram_id", "parent_username", "balance_date"),
        "edit": ("group_id", "full_name", "parent_telegram_id", "parent_username", "balance_date"),
        "int_fields": {"group_id", "parent_telegram_id"},
    },
    "attendance": {
        "pk": ("id",),
        "add": ("student_id", "date", "status"),
        "edit": ("student_id", "date", "status"),
        "int_fields": {"student_id"},
    },
}


def _managed_table(table: str) -> dict:
    if table not in _MANAGED_TABLES:
        raise ValueError(f"Unsupported table: {table}")
    return _MANAGED_TABLES[table]


def _managed_key_values(table: str, key: str) -> tuple[str, ...]:
    metadata = _managed_table(table)
    if len(metadata["pk"]) == 1:
        return (key,)
    values = tuple(key.split("_"))
    if len(values) != len(metadata["pk"]):
        raise ValueError("Invalid composite record key")
    return values


async def list_managed_records(
    table: str, limit: int, offset: int
) -> list[aiosqlite.Row]:
    _managed_table(table)
    async with _connect() as db:
        cursor = await db.execute(
            f"SELECT * FROM {table} ORDER BY rowid LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return await cursor.fetchall()


async def get_managed_record(table: str, key: str) -> aiosqlite.Row | None:
    metadata = _managed_table(table)
    key_values = _managed_key_values(table, key)
    where = " AND ".join(f"{field} = ?" for field in metadata["pk"])
    async with _connect() as db:
        cursor = await db.execute(
            f"SELECT * FROM {table} WHERE {where}", key_values
        )
        return await cursor.fetchone()


async def insert_managed_record(table: str, values: dict[str, str]) -> None:
    metadata = _managed_table(table)
    fields = metadata["add"]
    if set(values) != set(fields):
        raise ValueError("Record fields do not match table schema")
    converted: list[str | int] = []
    for field in fields:
        value = values[field].strip()
        if not value and field not in {
            "username", "parent_username", "admin_username"
        }:
            raise ValueError(f"{field} cannot be empty")
        if value and field in metadata["int_fields"]:
            converted.append(int(value))
        else:
            converted.append(value)
    if table == "group_staff":
        async with _connect() as db:
            cursor = await db.execute(
                """SELECT 1 FROM groups g
                   JOIN staff s ON s.center_id = g.center_id
                   WHERE g.id = ? AND s.telegram_id = ? AND s.role = 'teacher'""",
                (int(values["group_id"]), int(values["telegram_id"])),
            )
            if not await cursor.fetchone():
                raise ValueError(
                    "Guruh va o'qituvchi bir markazga tegishli bo'lishi kerak."
                )
            await db.execute(
                "INSERT INTO group_staff(group_id, telegram_id) VALUES (?, ?)",
                converted,
            )
            await db.commit()
        return
    placeholders = ", ".join("?" for _ in fields)
    columns = ", ".join(fields)
    async with _connect() as db:
        await db.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            converted,
        )
        await db.commit()


async def update_managed_record(
    table: str, key: str, field: str, value: str
) -> None:
    metadata = _managed_table(table)
    if field not in metadata["edit"]:
        raise ValueError(f"Field {field} cannot be edited in {table}")
    value = value.strip()
    if not value and field not in {
        "username", "parent_username", "admin_username"
    }:
        raise ValueError(f"{field} cannot be empty")
    if value and field in metadata["int_fields"]:
        converted: str | int = int(value)
    else:
        converted = value
    key_values = _managed_key_values(table, key)
    where = " AND ".join(f"{name} = ?" for name in metadata["pk"])
    async with _connect() as db:
        old_record = None
        if table == "staff":
            cursor = await db.execute(
                f"SELECT * FROM staff WHERE {where}", key_values
            )
            old_record = await cursor.fetchone()
        await db.execute(
            f"UPDATE {table} SET {field} = ? WHERE {where}",
            (converted, *key_values),
        )
        if table == "staff" and old_record is not None:
            old_center = old_record["center_id"]
            old_telegram = old_record["telegram_id"]
            cursor = await db.execute(
                "SELECT * FROM staff WHERE id = ?", (old_record["id"],)
            )
            new_record = await cursor.fetchone()
            keep_assignments = (
                old_record["role"] == "teacher"
                and new_record["role"] == "teacher"
                and old_center == new_record["center_id"]
            )
            if keep_assignments:
                await db.execute(
                    """INSERT OR IGNORE INTO group_staff(group_id, telegram_id)
                       SELECT group_id, ? FROM group_staff gs
                       JOIN groups g ON g.id = gs.group_id
                       WHERE gs.telegram_id = ? AND g.center_id = ?""",
                    (
                        new_record["telegram_id"],
                        old_telegram,
                        old_center,
                    ),
                )
            await db.execute(
                """DELETE FROM group_staff
                   WHERE telegram_id = ?
                     AND group_id IN (SELECT id FROM groups WHERE center_id = ?)""",
                (old_telegram, old_center),
            )
        if table == "groups" and field == "center_id":
            await db.execute(
                """DELETE FROM group_staff
                   WHERE group_id = ?
                     AND telegram_id NOT IN (
                         SELECT telegram_id FROM staff
                         WHERE center_id = ? AND role = 'teacher'
                     )""",
                (int(key_values[0]), converted),
            )
        await db.commit()


async def delete_managed_record(table: str, key: str) -> None:
    metadata = _managed_table(table)
    key_values = _managed_key_values(table, key)
    where = " AND ".join(f"{name} = ?" for name in metadata["pk"])
    async with _connect() as db:
        if table == "staff":
            cursor = await db.execute(
                "SELECT center_id, telegram_id FROM staff WHERE id = ?",
                key_values,
            )
            staff_record = await cursor.fetchone()
            if staff_record:
                await db.execute(
                    """DELETE FROM group_staff
                       WHERE telegram_id = ?
                         AND group_id IN (
                             SELECT id FROM groups WHERE center_id = ?
                         )""",
                    (staff_record["telegram_id"], staff_record["center_id"]),
                )
        await db.execute(f"DELETE FROM {table} WHERE {where}", key_values)
        await db.commit()


async def clear_managed_data() -> None:
    async with _connect() as db:
        await db.execute("DELETE FROM attendance")
        await db.execute("DELETE FROM students")
        await db.execute("DELETE FROM group_staff")
        await db.execute("DELETE FROM groups")
        await db.execute("DELETE FROM staff")
        await db.execute("DELETE FROM centers")
        await db.commit()


async def set_center_active(center_id: int, is_active: bool) -> None:
    async with _connect() as db:
        await db.execute(
            "UPDATE centers SET is_active = ? WHERE id = ?",
            (int(is_active), center_id),
        )
        await db.commit()


async def get_center(center_id: int) -> aiosqlite.Row | None:
    async with _connect() as db:
        cursor = await db.execute("SELECT * FROM centers WHERE id = ?", (center_id,))
        return await cursor.fetchone()


async def centers_for_user(telegram_id: int) -> list[aiosqlite.Row]:
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT c.* FROM centers c
               JOIN staff s ON s.center_id = c.id
               WHERE s.telegram_id = ? ORDER BY c.name""",
            (telegram_id,),
        )
        return await cursor.fetchall()


async def staff_role(center_id: int, telegram_id: int) -> str | None:
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT role FROM staff WHERE center_id = ? AND telegram_id = ?",
            (center_id, telegram_id),
        )
        row = await cursor.fetchone()
        return row["role"] if row else None


async def update_staff_full_name(
    center_id: int, telegram_id: int, full_name: str, username: str = ""
) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE staff SET full_name = CASE WHEN full_name = '' THEN ? ELSE full_name END,
               username = CASE WHEN username = '' THEN ? ELSE username END
               WHERE center_id = ? AND telegram_id = ?""",
            (full_name, username, center_id, telegram_id),
        )
        if username:
            await db.execute(
                """UPDATE centers SET admin_username = ?
                   WHERE id = ? AND admin_telegram = ? AND admin_username = ''""",
                (username, center_id, telegram_id),
            )
        await db.commit()


async def add_teacher(
    center_id: int, telegram_id: int, full_name: str, username: str = ""
) -> None:
    async with _connect() as db:
        await db.execute(
            """INSERT INTO staff(center_id, telegram_id, full_name, username, role)
               VALUES (?, ?, ?, ?, 'teacher')
               ON CONFLICT(center_id, telegram_id) DO UPDATE
               SET full_name = excluded.full_name,
                   username = CASE WHEN excluded.username = ''
                                   THEN staff.username ELSE excluded.username END
               WHERE staff.role = 'teacher'""",
            (center_id, telegram_id, full_name, username),
        )
        await db.commit()


async def list_groups(center_id: int) -> list[aiosqlite.Row]:
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT * FROM groups WHERE center_id = ? ORDER BY name", (center_id,)
        )
        return await cursor.fetchall()


async def get_group(group_id: int) -> aiosqlite.Row | None:
    async with _connect() as db:
        cursor = await db.execute("SELECT * FROM groups WHERE id = ?", (group_id,))
        return await cursor.fetchone()


async def create_group(center_id: int, name: str) -> None:
    async with _connect() as db:
        await db.execute(
            "INSERT INTO groups(center_id, name) VALUES (?, ?)", (center_id, name)
        )
        await db.commit()


async def list_groups_for_user(
    center_id: int, telegram_id: int, role: str
) -> list[aiosqlite.Row]:
    async with _connect() as db:
        if role == "admin":
            cursor = await db.execute(
                "SELECT * FROM groups WHERE center_id = ? ORDER BY name",
                (center_id,),
            )
        else:
            cursor = await db.execute(
                """SELECT g.* FROM groups g
                   JOIN group_staff gs ON gs.group_id = g.id
                   WHERE g.center_id = ? AND gs.telegram_id = ?
                   ORDER BY g.name""",
                (center_id, telegram_id),
            )
        return await cursor.fetchall()


async def group_role(center_id: int, group_id: int, telegram_id: int) -> str | None:
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT s.role FROM staff s
               JOIN groups g ON g.center_id = s.center_id
               WHERE s.center_id = ? AND g.id = ? AND s.telegram_id = ?
                 AND (
                     s.role = 'admin'
                     OR EXISTS (
                         SELECT 1 FROM group_staff gs
                         WHERE gs.group_id = g.id AND gs.telegram_id = s.telegram_id
                     )
                 )""",
            (center_id, group_id, telegram_id),
        )
        row = await cursor.fetchone()
        return row["role"] if row else None


async def list_center_teachers(center_id: int) -> list[aiosqlite.Row]:
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT telegram_id, full_name, username FROM staff
               WHERE center_id = ? AND role = 'teacher'
               ORDER BY telegram_id""",
            (center_id,),
        )
        return await cursor.fetchall()


async def list_center_staff_page(
    center_id: int, limit: int, offset: int
) -> list[aiosqlite.Row]:
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT full_name, telegram_id, username, role FROM staff
               WHERE center_id = ?
               ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, full_name, telegram_id
               LIMIT ? OFFSET ?""",
            (center_id, limit, offset),
        )
        return await cursor.fetchall()


async def list_group_staff_details(group_id: int) -> list[aiosqlite.Row]:
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT s.full_name, s.telegram_id, s.username, s.role
               FROM group_staff gs
               JOIN staff s ON s.telegram_id = gs.telegram_id
               JOIN groups g ON g.id = gs.group_id AND g.center_id = s.center_id
               WHERE gs.group_id = ?
               ORDER BY s.full_name, s.telegram_id""",
            (group_id,),
        )
        return await cursor.fetchall()


async def group_teacher_ids(group_id: int) -> set[int]:
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT telegram_id FROM group_staff WHERE group_id = ?",
            (group_id,),
        )
        return {row["telegram_id"] for row in await cursor.fetchall()}


async def set_group_teacher(
    center_id: int, group_id: int, telegram_id: int, assigned: bool
) -> bool:
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT 1 FROM groups g JOIN staff s ON s.center_id = g.center_id
               WHERE g.id = ? AND g.center_id = ? AND s.telegram_id = ?
                 AND s.role = 'teacher'""",
            (group_id, center_id, telegram_id),
        )
        if not await cursor.fetchone():
            return False
        if assigned:
            await db.execute(
                "INSERT OR IGNORE INTO group_staff(group_id, telegram_id) VALUES (?, ?)",
                (group_id, telegram_id),
            )
        else:
            await db.execute(
                "DELETE FROM group_staff WHERE group_id = ? AND telegram_id = ?",
                (group_id, telegram_id),
            )
        await db.commit()
        return True


async def list_students(group_id: int) -> list[aiosqlite.Row]:
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT * FROM students WHERE group_id = ? ORDER BY full_name", (group_id,)
        )
        return await cursor.fetchall()


async def get_student(student_id: int) -> aiosqlite.Row | None:
    async with _connect() as db:
        cursor = await db.execute("SELECT * FROM students WHERE id = ?", (student_id,))
        return await cursor.fetchone()


async def create_student(
    group_id: int,
    full_name: str,
    parent_id: int,
    balance_date: str,
    parent_username: str = "",
) -> None:
    async with _connect() as db:
        await db.execute(
            """INSERT INTO students(
                   group_id, full_name, parent_telegram_id, parent_username, balance_date
               ) VALUES (?, ?, ?, ?, ?)""",
            (group_id, full_name, parent_id, parent_username, balance_date),
        )
        await db.commit()


async def update_student(student_id: int, field: str, value: str | int) -> None:
    allowed_fields = {
        "full_name", "parent_telegram_id", "parent_username", "balance_date"
    }
    if field not in allowed_fields:
        raise ValueError(f"Unsupported student field: {field}")
    async with _connect() as db:
        await db.execute(
            f"UPDATE students SET {field} = ? WHERE id = ?", (value, student_id)
        )
        await db.commit()


async def delete_student(student_id: int) -> None:
    async with _connect() as db:
        await db.execute("DELETE FROM students WHERE id = ?", (student_id,))
        await db.commit()


async def save_attendance(
    attendance_date: str, statuses: dict[int, str]
) -> None:
    async with _connect() as db:
        await db.executemany(
            """INSERT INTO attendance(student_id, date, status) VALUES (?, ?, ?)
               ON CONFLICT(student_id, date) DO UPDATE SET status = excluded.status""",
            [
                (student_id, attendance_date, status)
                for student_id, status in statuses.items()
            ],
        )
        await db.commit()


async def students_for_reminders() -> list[aiosqlite.Row]:
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT s.id, s.full_name, s.parent_telegram_id, s.parent_username,
                      s.balance_date, g.name AS group_name,
                      c.id AS center_id, c.name AS center_name, c.card_number,
                      c.card_holder, c.admin_telegram, c.admin_username,
                      staff.username AS stored_admin_username
               FROM students s
               JOIN groups g ON g.id = s.group_id
               JOIN centers c ON c.id = g.center_id
               LEFT JOIN staff ON staff.center_id = c.id
                              AND staff.telegram_id = c.admin_telegram
                              AND staff.role = 'admin'
               WHERE c.is_active = 1
               ORDER BY c.id, s.full_name"""
        )
        return await cursor.fetchall()
