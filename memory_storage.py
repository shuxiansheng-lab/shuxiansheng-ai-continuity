"""
memory_storage.py — 书先生的记忆数据库

SQLite layer for memories. The ONLY module that touches shuxiansheng.db.
Replaces memories.json with proper database operations.

Usage:
    from memory_storage import MemoryDB
    db = MemoryDB()                          # uses default path
    db = MemoryDB("/path/to/shuxiansheng.db")  # custom path

All public methods return plain dicts or lists of dicts — no ORM, no magic.
"""

import sqlite3
import uuid
import hashlib
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_DB = Path(__file__).parent / "shuxiansheng.db"


class MemoryDB:
    def __init__(self, db_path=None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ──────────────────────────────────
    #  Connection & schema
    # ──────────────────────────────────

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = self._dict_factory
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _dict_factory(cursor, row):
        return {col[0]: row[i] for i, col in enumerate(cursor.description)}

    def _init_db(self):
        conn = self._connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id              TEXT PRIMARY KEY,
                content         TEXT NOT NULL,
                category        TEXT DEFAULT 'general',
                pinned          INTEGER DEFAULT 0,
                access_count    INTEGER DEFAULT 0,
                status          TEXT DEFAULT 'active',
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_mem_pinned ON memories(pinned);
            CREATE INDEX IF NOT EXISTS idx_mem_category ON memories(category);
            CREATE INDEX IF NOT EXISTS idx_mem_status ON memories(status);
            CREATE INDEX IF NOT EXISTS idx_mem_access ON memories(access_count);
            CREATE INDEX IF NOT EXISTS idx_mem_created ON memories(created_at);

            CREATE TABLE IF NOT EXISTS journal (
                id              TEXT PRIMARY KEY,
                content         TEXT NOT NULL,
                created_at      TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_journal_created ON journal(created_at);
        """)
        conn.commit()
        conn.close()

    # ──────────────────────────────────
    #  Helpers
    # ──────────────────────────────────

    @staticmethod
    def _new_id():
        return "mem_" + uuid.uuid4().hex[:12]

    @staticmethod
    def _now():
        return datetime.now().strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _content_hash(text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    # ──────────────────────────────────
    #  Add / pin / update
    # ──────────────────────────────────

    def add_memory(self, content, category="general", pinned=False, created_at=None):
        """Add one memory. Returns the new id."""
        conn = self._connect()
        now = self._now()
        mem_id = self._new_id()
        conn.execute(
            "INSERT INTO memories (id, content, category, pinned, access_count, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, 'active', ?, ?)",
            [mem_id, content, category, 1 if pinned else 0, created_at or now, now]
        )
        conn.commit()
        conn.close()
        return mem_id

    def pin_by_keyword(self, keyword, unpin=False):
        """Pin/unpin all active memories matching keyword. Returns list of matched content snippets."""
        conn = self._connect()
        cursor = conn.execute(
            "SELECT id, content FROM memories WHERE status = 'active' AND content LIKE ?",
            [f"%{keyword}%"]
        )
        matched = cursor.fetchall()
        if matched:
            ids = [m["id"] for m in matched]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE memories SET pinned = ?, updated_at = ? WHERE id IN ({placeholders})",
                [0 if unpin else 1, self._now()] + ids
            )
            conn.commit()
        conn.close()
        return [m["content"][:30] for m in matched]

    def pin_by_id(self, mem_id, unpin=False):
        """Pin/unpin a single memory by id."""
        conn = self._connect()
        conn.execute(
            "UPDATE memories SET pinned = ?, updated_at = ? WHERE id = ?",
            [0 if unpin else 1, self._now(), mem_id]
        )
        conn.commit()
        conn.close()

    def increment_access(self, mem_ids):
        """Bump access_count for a list of memory ids."""
        if not mem_ids:
            return
        conn = self._connect()
        placeholders = ",".join("?" for _ in mem_ids)
        conn.execute(
            f"UPDATE memories SET access_count = access_count + 1 WHERE id IN ({placeholders})",
            mem_ids
        )
        conn.commit()
        conn.close()

    # ──────────────────────────────────
    #  Query — for build_prompt
    # ──────────────────────────────────

    def get_pinned(self):
        """All pinned active memories."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM memories WHERE status = 'active' AND pinned = 1 ORDER BY created_at"
        ).fetchall()
        conn.close()
        return rows

    def get_recent_digests(self, limit=3):
        """Most recent daily digests."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM memories WHERE status = 'active' AND category = 'digest' "
            "ORDER BY created_at DESC LIMIT ?",
            [limit]
        ).fetchall()
        conn.close()
        return list(reversed(rows))  # oldest first for display

    def get_recent_memories(self, limit=15):
        """Recent non-pinned, non-digest active memories."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM memories WHERE status = 'active' AND pinned = 0 AND category != 'digest' "
            "ORDER BY created_at DESC LIMIT ?",
            [limit]
        ).fetchall()
        conn.close()
        return list(reversed(rows))  # oldest first for display

    def search_by_keywords(self, keywords, limit=8, exclude_ids=None):
        """Search active non-pinned, non-digest memories by keyword matching.
        Returns memories that match any keyword, sorted by match count desc."""
        if not keywords:
            return []
        conn = self._connect()
        exclude_ids = exclude_ids or []

        # Build CASE scoring: each keyword hit adds 1
        score_parts = []
        params = []
        for kw in keywords:
            score_parts.append("(CASE WHEN content LIKE ? THEN 1 ELSE 0 END)")
            params.append(f"%{kw}%")
        score_expr = " + ".join(score_parts)

        # Exclude clause
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            exclude_clause = f"AND id NOT IN ({placeholders})"
            params.extend(exclude_ids)
        else:
            exclude_clause = ""

        # At least one keyword must match
        match_parts = " OR ".join("content LIKE ?" for _ in keywords)
        for kw in keywords:
            params.append(f"%{kw}%")

        params.append(limit)

        sql = f"""
            SELECT *, ({score_expr}) as match_score
            FROM memories
            WHERE status = 'active' AND pinned = 0 AND category != 'digest'
            {exclude_clause}
            AND ({match_parts})
            ORDER BY match_score DESC, created_at DESC
            LIMIT ?
        """
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return rows

    def get_random_old(self, before_date, limit=2, exclude_ids=None):
        """Random old memories from before a given date (for 随机浮现).
        Excludes memories already shown in recent_memories."""
        conn = self._connect()
        exclude_ids = exclude_ids or []

        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            rows = conn.execute(
                f"SELECT * FROM memories WHERE status = 'active' AND pinned = 0 "
                f"AND category != 'digest' AND created_at < ? "
                f"AND id NOT IN ({placeholders}) "
                f"ORDER BY RANDOM() LIMIT ?",
                [before_date] + exclude_ids + [limit]
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memories WHERE status = 'active' AND pinned = 0 "
                "AND category != 'digest' AND created_at < ? "
                "ORDER BY RANDOM() LIMIT ?",
                [before_date, limit]
            ).fetchall()

        conn.close()
        return rows

    def get_all_active(self):
        """All active memories, for daily_review context and API display."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM memories WHERE status = 'active' ORDER BY created_at"
        ).fetchall()
        conn.close()
        return rows

    def get_recent_n(self, limit=10):
        """Most recent N active memories (for initiative/self-trigger context)."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM memories WHERE status = 'active' ORDER BY created_at DESC LIMIT ?",
            [limit]
        ).fetchall()
        conn.close()
        return list(reversed(rows))

    def count_active(self):
        """Count of active memories."""
        conn = self._connect()
        row = conn.execute("SELECT COUNT(*) as n FROM memories WHERE status = 'active'").fetchone()
        conn.close()
        return row["n"]

    # ──────────────────────────────────
    #  Consolidation
    # ──────────────────────────────────

    def get_consolidation_candidates(self):
        """Unpinned, access_count < 5, non-digest active memories — eligible for cleanup."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM memories WHERE status = 'active' AND pinned = 0 "
            "AND access_count < 5 AND category != 'digest' "
            "ORDER BY created_at"
        ).fetchall()
        conn.close()
        return rows

    def get_protected(self):
        """Unpinned but frequently accessed (access_count >= 5), non-digest."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM memories WHERE status = 'active' AND pinned = 0 "
            "AND access_count >= 5 AND category != 'digest' "
            "ORDER BY created_at"
        ).fetchall()
        conn.close()
        return rows

    def replace_candidates(self, old_ids, kept_memories, digest_text=None):
        """Archive old candidates and insert kept + digest.
        
        Args:
            old_ids: list of memory ids to archive
            kept_memories: list of dicts with 'content', 'category', 'time' (from Claude)
            digest_text: optional consolidation digest string
        """
        conn = self._connect()
        now = self._now()

        # Archive old candidates
        if old_ids:
            placeholders = ",".join("?" for _ in old_ids)
            conn.execute(
                f"UPDATE memories SET status = 'archived', updated_at = ? WHERE id IN ({placeholders})",
                [now] + old_ids
            )

        # Insert kept memories
        for m in kept_memories:
            conn.execute(
                "INSERT INTO memories (id, content, category, pinned, access_count, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 0, 0, 'active', ?, ?)",
                [self._new_id(), m["content"], m.get("category", "general"), m.get("time", now), now]
            )

        # Insert digest
        if digest_text:
            conn.execute(
                "INSERT INTO memories (id, content, category, pinned, access_count, status, created_at, updated_at) "
                "VALUES (?, ?, 'digest', 0, 0, 'active', ?, ?)",
                [self._new_id(), digest_text, now, now]
            )

        conn.commit()
        conn.close()

    # ──────────────────────────────────
    #  Admin
    # ──────────────────────────────────

    def delete_all(self):
        """Clear all memories (for DELETE /api/memories)."""
        conn = self._connect()
        conn.execute("DELETE FROM memories")
        conn.commit()
        conn.close()

    def get_by_index(self, index):
        """Get memory by display index (position in all active, sorted by created_at).
        Returns (id, memory_dict) or (None, None)."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM memories WHERE status = 'active' ORDER BY created_at"
        ).fetchall()
        conn.close()
        if 0 <= index < len(rows):
            return rows[index]["id"], rows[index]
        return None, None

    def toggle_pin_by_index(self, index):
        """Toggle pin for memory at display index. Returns (ok, status)."""
        mem_id, mem = self.get_by_index(index)
        if mem_id is None:
            return False, "out_of_range"
        new_pinned = 0 if mem["pinned"] else 1
        conn = self._connect()
        conn.execute(
            "UPDATE memories SET pinned = ?, updated_at = ? WHERE id = ?",
            [new_pinned, self._now(), mem_id]
        )
        conn.commit()
        conn.close()
        return True, "pinned" if new_pinned else "unpinned"

    # ──────────────────────────────────
    #  Journal — 书先生自己写的东西
    # ──────────────────────────────────

    def add_journal(self, content):
        """Write a journal entry. Returns the new id."""
        conn = self._connect()
        entry_id = "j_" + uuid.uuid4().hex[:12]
        conn.execute(
            "INSERT INTO journal (id, content, created_at) VALUES (?, ?, ?)",
            [entry_id, content, self._now()]
        )
        conn.commit()
        conn.close()
        return entry_id

    def get_recent_journal(self, limit=5):
        """Most recent journal entries."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM journal ORDER BY created_at DESC LIMIT ?",
            [limit]
        ).fetchall()
        conn.close()
        return list(reversed(rows))  # oldest first for display

    def get_random_old_journal(self, before_date, limit=1, exclude_ids=None):
        """Random old journal entries from before a given date."""
        conn = self._connect()
        exclude_ids = exclude_ids or []
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            rows = conn.execute(
                f"SELECT * FROM journal WHERE created_at < ? AND id NOT IN ({placeholders}) "
                "ORDER BY RANDOM() LIMIT ?",
                [before_date] + exclude_ids + [limit]
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM journal WHERE created_at < ? ORDER BY RANDOM() LIMIT ?",
                [before_date, limit]
            ).fetchall()
        conn.close()
        return rows

    def count_journal(self):
        """Count of journal entries."""
        conn = self._connect()
        row = conn.execute("SELECT COUNT(*) as n FROM journal").fetchone()
        conn.close()
        return row["n"]


# ══════════════════════════════════════
#  Vector Search — TF-IDF with character n-grams
# ══════════════════════════════════════

import math

# Common Chinese particles/stop words to strip before vectorizing
_STOP_CHARS = set("的了吗呢吧啊呀哦嗯是在有不也都就会要这那我你他她它们和与或但如果所以虽然可以能够已经还没很太最更比较")


class MemorySearcher:
    """TF-IDF vector search over memories. Pure Python, zero dependencies.

    Uses character bigrams + trigrams as features, IDF weighting across
    all memories, and cosine similarity for ranking. Handles Chinese
    natively since n-grams don't need word segmentation.

    Usage:
        searcher = MemorySearcher(memory_db)
        results = searcher.search("用户喜欢喝茶", top_k=8)
        # returns [(id, score, content), ...]
    """

    def __init__(self, db):
        self.db = db
        self._vectors = {}   # id -> {ngram: tf}
        self._contents = {}  # id -> content string
        self._idf = {}       # ngram -> idf weight
        self._dirty = True

    @staticmethod
    def _clean(text):
        """Strip stop chars and whitespace."""
        return "".join(c for c in text if c not in _STOP_CHARS and not c.isspace())

    @staticmethod
    def _ngrams(cleaned, ns=(2, 3)):
        """Generate character n-grams."""
        grams = []
        for n in ns:
            for i in range(len(cleaned) - n + 1):
                gram = cleaned[i:i + n]
                if gram.strip():
                    grams.append(gram)
        return grams

    def _vectorize(self, text):
        """Convert text to sparse TF vector."""
        cleaned = self._clean(text)
        grams = self._ngrams(cleaned)
        vec = {}
        for g in grams:
            vec[g] = vec.get(g, 0) + 1
        total = sum(vec.values()) or 1
        return {k: v / total for k, v in vec.items()}

    def rebuild(self):
        """Rebuild the full index from all active memories."""
        memories = self.db.get_all_active()
        self._vectors = {}
        self._contents = {}
        self._dates = {}
        doc_freq = {}

        for m in memories:
            vec = self._vectorize(m["content"])
            self._vectors[m["id"]] = vec
            self._contents[m["id"]] = m["content"]
            self._dates[m["id"]] = m.get("created_at", "")
            for gram in vec:
                doc_freq[gram] = doc_freq.get(gram, 0) + 1

        n_docs = len(memories) or 1
        self._idf = {
            gram: math.log(n_docs / (freq + 1)) + 1
            for gram, freq in doc_freq.items()
        }
        self._dirty = False

    def search(self, query_text, top_k=8, exclude_ids=None):
        """Search memories by vector similarity.

        Returns list of (id, score, content) tuples, sorted by score desc.
        Score range: 0.0 ~ 1.0 (cosine similarity with IDF weighting).
        """
        if self._dirty or not self._vectors:
            self.rebuild()

        exclude_ids = set(exclude_ids or [])
        q_vec = self._vectorize(query_text)

        # Apply IDF to query
        q_w = {g: tf * self._idf.get(g, 1.0) for g, tf in q_vec.items()}
        q_norm = math.sqrt(sum(v * v for v in q_w.values())) or 1e-9

        results = []
        for mem_id, mem_vec in self._vectors.items():
            if mem_id in exclude_ids:
                continue

            # Apply IDF to memory
            m_w = {g: tf * self._idf.get(g, 1.0) for g, tf in mem_vec.items()}
            m_norm = math.sqrt(sum(v * v for v in m_w.values())) or 1e-9

            # Cosine similarity (only compute dot product on shared keys)
            shared = set(q_w) & set(m_w)
            if not shared:
                continue
            dot = sum(q_w[g] * m_w[g] for g in shared)
            sim = dot / (q_norm * m_norm)

            if sim > 0.05:
                results.append((mem_id, sim, self._contents.get(mem_id, ""), self._dates.get(mem_id, "")))

        results.sort(key=lambda x: -x[1])
        return results[:top_k]

    def mark_dirty(self):
        """Call after memories are added, modified, or deleted."""
        self._dirty = True
