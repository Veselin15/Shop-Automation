"""SQLite слой. Ботът трябва да е рестартируем по всяко време без загуба на състояние."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .models import AdStats, Listing, ListingState, Product, ProductStatus, Size, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    brand           TEXT DEFAULT '',
    name            TEXT DEFAULT '',
    category_key    TEXT DEFAULT '',
    price           REAL DEFAULT 0,
    orig_price      REAL DEFAULT 0,
    currency        TEXT DEFAULT 'EUR',
    description     TEXT DEFAULT '',
    color           TEXT DEFAULT '',
    material        TEXT DEFAULT '',
    images          TEXT DEFAULT '[]',
    sizes           TEXT DEFAULT '[]',
    status          TEXT DEFAULT 'available',
    bestseller      INTEGER DEFAULT 0,
    low_stock       INTEGER DEFAULT 0,
    listing_rank    INTEGER DEFAULT 9999,
    score           REAL DEFAULT 0,
    first_seen      TEXT,
    last_seen       TEXT,
    last_checked    TEXT,
    miss_count      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS listings (
    product_id      TEXT PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    bazar_id        TEXT,
    bazar_url       TEXT,
    title           TEXT DEFAULT '',
    price           REAL DEFAULT 0,
    currency        TEXT DEFAULT 'EUR',
    category_id     INTEGER DEFAULT 0,
    state           TEXT DEFAULT 'candidate',
    content_hash    TEXT DEFAULT '',
    attempts        INTEGER DEFAULT 0,
    last_error      TEXT DEFAULT '',
    published_at    TEXT,
    publish_discount_pct INTEGER DEFAULT 0,
    removed_at      TEXT,
    removal_reason  TEXT DEFAULT '',
    views           INTEGER DEFAULT 0,
    phones          INTEGER DEFAULT 0,
    favorites       INTEGER DEFAULT 0,
    stats_at        TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    product_id  TEXT,
    message     TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS appraisals (
    product_id  TEXT PRIMARY KEY,
    score       REAL DEFAULT 0,
    reason      TEXT DEFAULT '',
    model       TEXT DEFAULT '',
    prompt      TEXT DEFAULT '',
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS counters (
    day     TEXT NOT NULL,
    kind    TEXT NOT NULL,
    count   INTEGER DEFAULT 0,
    PRIMARY KEY (day, kind)
);

CREATE INDEX IF NOT EXISTS idx_listings_state ON listings(state);
CREATE INDEX IF NOT EXISTS idx_products_checked ON products(last_checked);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


# Опити за публикуване на една обява, преди да се откажем от нея.
MAX_PUBLISH_ATTEMPTS = 3


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Дребни промени по схемата на вече съществуваща база."""
        appraisal_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(appraisals)")}
        if appraisal_cols and "prompt" not in appraisal_cols:
            self.conn.execute("ALTER TABLE appraisals ADD COLUMN prompt TEXT DEFAULT ''")

        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(listings)")}
        if "publish_discount_pct" not in columns:
            self.conn.execute(
                "ALTER TABLE listings ADD COLUMN publish_discount_pct INTEGER DEFAULT 0"
            )
        for column, kind in (("views", "INTEGER DEFAULT 0"), ("phones", "INTEGER DEFAULT 0"),
                             ("favorites", "INTEGER DEFAULT 0"), ("stats_at", "TEXT")):
            if column not in columns:
                self.conn.execute(f"ALTER TABLE listings ADD COLUMN {column} {kind}")
        # Категорията беше текстов път ("Мода > Аксесоари"), сега е числово id.
        if "category_label" in columns and "category_id" not in columns:
            self.conn.execute("ALTER TABLE listings DROP COLUMN category_label")
            self.conn.execute(
                "ALTER TABLE listings ADD COLUMN category_id INTEGER DEFAULT 0"
            )

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------------- products ----------------

    def upsert_product(self, p: Product, score: float = 0.0) -> None:
        now = utcnow()
        with self.tx() as c:
            c.execute(
                """
                INSERT INTO products (id, url, brand, name, category_key, price, orig_price,
                    currency, description, color, material, images, sizes, status,
                    bestseller, low_stock, listing_rank, score,
                    first_seen, last_seen, last_checked, miss_count)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                ON CONFLICT(id) DO UPDATE SET
                    url=excluded.url, brand=excluded.brand, name=excluded.name,
                    category_key=excluded.category_key, price=excluded.price,
                    orig_price=excluded.orig_price, currency=excluded.currency,
                    description=excluded.description, color=excluded.color,
                    material=excluded.material, images=excluded.images, sizes=excluded.sizes,
                    status=excluded.status, bestseller=excluded.bestseller,
                    low_stock=excluded.low_stock, listing_rank=excluded.listing_rank,
                    score=excluded.score, last_seen=excluded.last_seen,
                    last_checked=excluded.last_checked, miss_count=0
                """,
                (
                    p.id, p.url, p.brand, p.name, p.category_key, p.price, p.orig_price,
                    p.currency, p.description, p.color, p.material,
                    json.dumps(p.images, ensure_ascii=False),
                    json.dumps([[s.label, s.available] for s in p.sizes], ensure_ascii=False),
                    str(p.status), int(p.bestseller_badge), int(p.low_stock), p.listing_rank,
                    score, now, now, now,
                ),
            )

    def mark_checked(self, product_id: str, status: ProductStatus, miss: bool = False) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE products SET last_checked=?, status=?, "
                "miss_count = CASE WHEN ? THEN miss_count + 1 ELSE 0 END WHERE id=?",
                (utcnow(), str(status), int(miss), product_id),
            )

    def update_prices(self, product_id: str, price: float, orig_price: float) -> None:
        """Непрочетената каталожна цена (0) не изтрива записаната.

        При проверка няма плочка, от която да се вземе резервна стойност, а
        нула тук значи намаление 0% и сваляне на обява, която е наред. Ако
        продуктът наистина е на пълна цена, цената се изравнява с каталожната
        и намалението пак излиза 0.
        """
        with self.tx() as c:
            c.execute(
                "UPDATE products SET price=?, "
                "orig_price = CASE WHEN ? > 0 THEN ? ELSE orig_price END, "
                "last_seen=? WHERE id=?",
                (price, orig_price, orig_price, utcnow(), product_id),
            )

    def get_product(self, product_id: str) -> Product | None:
        row = self.conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        return _row_to_product(row) if row else None

    def known_product_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT id FROM products")}

    def miss_count(self, product_id: str) -> int:
        row = self.conn.execute(
            "SELECT miss_count FROM products WHERE id=?", (product_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    # ---------------- listings ----------------

    def get_listing(self, product_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM listings WHERE product_id=?", (product_id,)
        ).fetchone()

    def save_candidate(self, listing: Listing) -> None:
        with self.tx() as c:
            c.execute(
                """
                INSERT INTO listings (product_id, title, price, currency, category_id,
                                      state, content_hash)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(product_id) DO UPDATE SET
                    title=excluded.title, price=excluded.price, currency=excluded.currency,
                    category_id=excluded.category_id, content_hash=excluded.content_hash
                WHERE listings.state IN ('candidate','failed')
                """,
                (
                    listing.product_id, listing.title, listing.price, listing.currency,
                    listing.category_id, str(ListingState.CANDIDATE), listing.content_hash,
                ),
            )

    def mark_published(self, product_id: str, bazar_id: str, bazar_url: str,
                       discount_pct: int = 0) -> None:
        """Запомня и намалението в деня на публикуване.

        То е базата, спрямо която после се съди дали офертата е още същата —
        абсолютен праг не различава 90% -> 70% от 50% -> 46%.
        """
        with self.tx() as c:
            c.execute(
                "UPDATE listings SET state=?, bazar_id=?, bazar_url=?, published_at=?, "
                "publish_discount_pct=?, last_error='' WHERE product_id=?",
                (str(ListingState.PUBLISHED), bazar_id, bazar_url, utcnow(),
                 discount_pct, product_id),
            )

    def mark_failed(self, product_id: str, error: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE listings SET state=?, attempts=attempts+1, last_error=? "
                "WHERE product_id=?",
                (str(ListingState.FAILED), error[:500], product_id),
            )

    def mark_removed(self, product_id: str, reason: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE listings SET state=?, removed_at=?, removal_reason=? WHERE product_id=?",
                (str(ListingState.REMOVED), utcnow(), reason, product_id),
            )

    def pending_candidates(self, limit: int | None = None) -> list[sqlite3.Row]:
        """Кандидатите, най-добре оцененото първо.

        Скорът за популярност мери марка и намаление и качва всяка позната
        марка нагоре; оценката гледа самия артикул. Затова води оценката, а
        старият скор остава само за да пререди равните.

        Това е редът вътре в категорията. Кое от категориите излиза решава
        `selection.fair_order` — само по оценка чантите с 0.75 винаги
        изпреварват часовниците с 0.55.
        """
        rows = list(
            self.conn.execute(
                """
                SELECT l.*, p.score AS score, p.category_key AS category_key,
                       COALESCE(a.score, -1) AS appraisal
                FROM listings l
                JOIN products p ON p.id = l.product_id
                LEFT JOIN appraisals a ON a.product_id = l.product_id
                WHERE l.state IN ('candidate','failed') AND l.attempts < ?
                ORDER BY appraisal DESC, p.score DESC, p.first_seen ASC
                """,
                (MAX_PUBLISH_ATTEMPTS,),
            )
        )
        return rows if limit is None else rows[:limit]

    def recent_publish_counts(self, window: int) -> dict[str, int]:
        """По категория: колко от последните `window` публикувани обяви са нейни.

        Свалените също се броят — мястото си в реда са го заели, когато са
        излезли.
        """
        rows = self.conn.execute(
            """
            SELECT p.category_key, COUNT(*)
            FROM (
                SELECT product_id FROM listings
                WHERE published_at IS NOT NULL
                ORDER BY published_at DESC LIMIT ?
            ) recent
            JOIN products p ON p.id = recent.product_id
            GROUP BY p.category_key
            """,
            (window,),
        )
        return {key: count for key, count in rows}

    def active_listings(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM listings WHERE state='published'"))

    def rotation_pool(self) -> list[sqlite3.Row]:
        """Активните обяви с категорията, оценката и интереса — за размяната."""
        return list(
            self.conn.execute(
                """
                SELECT l.*, p.category_key AS category_key,
                       COALESCE(a.score, -1) AS appraisal
                FROM listings l
                JOIN products p ON p.id = l.product_id
                LEFT JOIN appraisals a ON a.product_id = l.product_id
                WHERE l.state = 'published' AND COALESCE(l.bazar_id, '') != ''
                """
            )
        )

    def listing_by_bazar_id(self, bazar_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM listings WHERE bazar_id=?", (bazar_id,)
        ).fetchone()

    def save_ad_stats(self, stats: Mapping[str, AdStats]) -> None:
        """Броячите от последното четене на „Моите обяви“.

        Обява, чиито броячи този път не се прочетоха, остава без тях — и
        размяната не я пипа. Стари нули не бива да свалят обява, за която
        междувременно някой е поискал телефона.
        """
        now = utcnow()
        with self.tx() as c:
            c.execute("UPDATE listings SET stats_at=NULL WHERE state='published'")
            c.executemany(
                "UPDATE listings SET views=?, phones=?, favorites=?, stats_at=? "
                "WHERE bazar_id=? AND state='published'",
                [(s.views, s.phones, s.favorites, now, bazar_id)
                 for bazar_id, s in stats.items()],
            )

    def active_listing_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM listings WHERE state='published'"
        ).fetchone()
        return int(row[0])

    def listings_due_for_recheck(self, min_age_h: int, limit: int) -> list[sqlite3.Row]:
        cutoff = (datetime.now(UTC) - timedelta(hours=min_age_h)).isoformat(
            timespec="seconds"
        )
        return list(
            self.conn.execute(
                """
                SELECT l.*, p.url AS product_url, p.last_checked AS last_checked
                FROM listings l JOIN products p ON p.id = l.product_id
                WHERE l.state='published' AND (p.last_checked IS NULL OR p.last_checked < ?)
                ORDER BY p.last_checked ASC LIMIT ?
                """,
                (cutoff, limit),
            )
        )

    # ---------------- appraisals ----------------

    def get_appraisal(self, product_id: str, prompt: str = "") -> sqlite3.Row | None:
        """Оценката се плаща веднъж — но само докато правилата са същите.

        Смениш ли текста, по който моделът съди, старите оценки вече отговарят
        на друг въпрос и мълчаливото им използване прикрива промяната.
        """
        cur = self.conn.execute(
            "SELECT * FROM appraisals WHERE product_id = ? AND prompt = ?",
            (product_id, prompt),
        )
        return cur.fetchone()

    def save_appraisal(self, product_id: str, score: float, reason: str,
                       model: str, prompt: str = "") -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO appraisals (product_id, score, reason, model, prompt, created_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(product_id) DO UPDATE SET "
                "score=excluded.score, reason=excluded.reason, model=excluded.model, "
                "prompt=excluded.prompt, created_at=excluded.created_at",
                (product_id, score, reason[:300], model, prompt, utcnow()),
            )

    # ---------------- events & counters ----------------

    def log_event(self, kind: str, message: str = "", product_id: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO events (ts, kind, product_id, message) VALUES (?,?,?,?)",
                (utcnow(), kind, product_id, message[:1000]),
            )

    def bump_counter(self, kind: str, amount: int = 1) -> int:
        today = date.today().isoformat()
        with self.tx() as c:
            c.execute(
                "INSERT INTO counters (day, kind, count) VALUES (?,?,?) "
                "ON CONFLICT(day, kind) DO UPDATE SET count = count + excluded.count",
                (today, kind, amount),
            )
        return self.counter(kind)

    def counter(self, kind: str) -> int:
        today = date.today().isoformat()
        row = self.conn.execute(
            "SELECT count FROM counters WHERE day=? AND kind=?", (today, kind)
        ).fetchone()
        return int(row[0]) if row else 0

    def recent_events(self, limit: int = 30) -> list[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        )


def _row_to_product(row: sqlite3.Row) -> Product:
    return Product(
        id=row["id"],
        url=row["url"],
        brand=row["brand"],
        name=row["name"],
        category_key=row["category_key"],
        price=row["price"],
        orig_price=row["orig_price"],
        currency=row["currency"],
        description=row["description"],
        color=row["color"],
        material=row["material"],
        images=json.loads(row["images"] or "[]"),
        sizes=[Size(label=s[0], available=bool(s[1])) for s in json.loads(row["sizes"] or "[]")],
        status=ProductStatus(row["status"]),
        bestseller_badge=bool(row["bestseller"]),
        low_stock=bool(row["low_stock"]),
        listing_rank=row["listing_rank"],
    )
