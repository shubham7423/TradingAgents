"""Append-only markdown decision log for TradingAgents."""

import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path

from tradingagents.agents.utils.rating import parse_rating


class TradingMemoryLog:
    """Markdown log of trading decisions and reflections."""

    _SEPARATOR = "\n\n<!-- ENTRY_END -->\n\n"
    _DECISION_RE = re.compile(r"DECISION:\n(.*?)(?=\nREFLECTION:|\Z)", re.DOTALL)
    _REFLECTION_RE = re.compile(r"REFLECTION:\n(.*?)$", re.DOTALL)
    _ID_RE = re.compile(r"<!-- decision_id: (.*?) -->")

    def __init__(self, config: dict = None):
        cfg = config or {}
        self._log_path = None
        path = cfg.get("memory_log_path")
        if path:
            self._log_path = Path(path).expanduser()
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._max_entries = cfg.get("memory_log_max_entries")

    @contextmanager
    def _file_lock(self):
        """Lock a sibling file so replacing the log never replaces its lock."""
        # ponytail: one lock serializes the small local log; shard only if measured write contention matters.
        lock_path = self._log_path.with_name(self._log_path.name + ".lock")
        with lock_path.open("a+b") as lock_file:
            if os.name == "nt":
                import msvcrt

                if os.fstat(lock_file.fileno()).st_size == 0:
                    lock_file.write(b"0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _mutate(self, transform):
        """Serialize a read/transform/atomic-replace mutation across processes."""
        if not self._log_path:
            return None
        with self._file_lock():
            text = self._log_path.read_text(encoding="utf-8") if self._log_path.exists() else ""
            new_text, result, changed = transform(text)
            if changed:
                temp_path = self._log_path.with_suffix(".tmp")
                if temp_path == self._log_path:
                    temp_path = self._log_path.with_name(self._log_path.name + ".tmp")
                temp_path.write_text(new_text, encoding="utf-8")
                temp_path.replace(self._log_path)
            return result

    @staticmethod
    def _decision_id(date, ticker, rating, decision):
        payload = json.dumps([date, ticker, rating, decision], separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def store_decision(
        self, ticker: str, trade_date: str, final_trade_decision: str, decision_id=None
    ) -> bool:
        """Append one pending decision, returning false when its identity exists."""
        if not self._log_path:
            return False
        rating = parse_rating(final_trade_decision)
        identity = decision_id if decision_id is not None else self._decision_id(
            trade_date, ticker, rating, final_trade_decision
        )

        def transform(text):
            entries = [self._parse_entry(raw) for raw in text.split(self._SEPARATOR) if raw.strip()]
            if any(entry and entry["decision_id"] == identity for entry in entries):
                return text, False, False
            tag = f"[{trade_date} | {ticker} | {rating} | pending]"
            block = f"{tag}\n\n<!-- decision_id: {identity} -->\nDECISION:\n{final_trade_decision}"
            return text + block + self._SEPARATOR, True, True

        return self._mutate(transform)

    def load_entries(self) -> list[dict]:
        if not self._log_path or not self._log_path.exists():
            return []
        text = self._log_path.read_text(encoding="utf-8")
        return [entry for raw in text.split(self._SEPARATOR) if (entry := self._parse_entry(raw))]

    def get_history(self, ticker=None, as_of=None) -> tuple[list[dict], list[str]]:
        if not self._log_path or not self._log_path.exists():
            return [], []
        entries, warnings = [], []
        for raw in self._log_path.read_text(encoding="utf-8").split(self._SEPARATOR):
            if not raw.strip():
                continue
            entry = self._parse_entry(raw)
            if entry is None:
                warnings.append("malformed memory entry")
                continue
            entries.append(entry)
        counts = {}
        for entry in entries:
            if entry["legacy_identity"]:
                counts[entry["decision_id"]] = counts.get(entry["decision_id"], 0) + 1
        ambiguous = {identity for identity, count in counts.items() if count > 1}
        if ambiguous:
            warnings.extend("ambiguous legacy identity: " + identity for identity in sorted(ambiguous))
        result = []
        for entry in entries:
            if ticker is not None and entry["ticker"] != ticker:
                continue
            if as_of is not None and entry["date"] > as_of:
                continue
            projected = dict(entry)
            if as_of is not None and entry["resolved"] and entry["resolved"] > as_of:
                projected.update(
                    pending=True, raw=None, alpha=None, holding=None, resolved=None,
                    benchmark=None, reflection="",
                )
            result.append(projected)
        return result, warnings

    def get_pending_entries(self) -> list[dict]:
        return [e for e in self.load_entries() if e.get("pending")]

    def get_past_context(
        self, ticker: str, n_same: int = 5, n_cross: int = 3, as_of: str | None = None
    ) -> str:
        entries = [e for e in self.load_entries() if not e.get("pending")]
        if as_of is not None:
            entries = [e for e in entries if e.get("resolved") and e["resolved"] <= as_of]
        if not entries:
            return ""
        same, cross = [], []
        for e in reversed(entries):
            if len(same) >= n_same and len(cross) >= n_cross:
                break
            if e["ticker"] == ticker and len(same) < n_same:
                same.append(e)
            elif e["ticker"] != ticker and len(cross) < n_cross:
                cross.append(e)
        if not same and not cross:
            return ""
        parts = []
        if same:
            parts.append(f"Past analyses of {ticker} (most recent first):")
            parts.extend(self._format_full(e) for e in same)
        if cross:
            parts.append("Recent cross-ticker lessons:")
            parts.extend(self._format_reflection_only(e) for e in cross)
        return "\n\n".join(parts)

    def resolve_decision(
        self, *, decision_id, ticker, trade_date, raw_return, alpha_return, holding_days,
        benchmark_name, resolution_date, reflection,
    ) -> str:
        if not self._log_path:
            return "missing"

        def transform(text):
            blocks = text.split(self._SEPARATOR)
            matches = []
            for index, block in enumerate(blocks):
                entry = self._parse_entry(block)
                if (entry and entry["decision_id"] == decision_id
                        and entry["ticker"] == ticker and entry["date"] == trade_date):
                    matches.append((index, block, entry))
            if not matches:
                return text, "missing", False
            if len(matches) > 1:
                return text, "ambiguous", False
            index, block, entry = matches[0]
            if not entry["pending"]:
                same = (
                    entry["raw"] == f"{raw_return:+.1%}"
                    and entry["alpha"] == f"{alpha_return:+.1%}"
                    and entry["holding"] == f"{holding_days}d"
                    and entry["benchmark"] == benchmark_name
                    and entry["resolved"] == resolution_date
                    and entry["reflection"] == reflection.strip()
                )
                return text, "identical" if same else "already_resolved", False
            fields = block.strip().splitlines()[0].strip()[1:-1].split("|")
            rating = fields[2].strip()
            tag = self._resolved_tag(
                trade_date, ticker, rating, f"{raw_return:+.1%}", f"{alpha_return:+.1%}",
                holding_days, resolution_date, benchmark_name,
            )
            body = block.strip().splitlines()[1:]
            blocks[index] = f"{tag}\n\n" + "\n".join(body).strip() + f"\n\nREFLECTION:\n{reflection}"
            blocks = self._apply_rotation(blocks)
            return self._SEPARATOR.join(blocks), "updated", True

        return self._mutate(transform)

    def update_with_outcome(
        self, ticker, trade_date, raw_return, alpha_return, holding_days, reflection,
        resolution_date=None,
    ) -> None:
        def transform(text):
            blocks = text.split(self._SEPARATOR)
            updated = False
            for index, block in enumerate(blocks):
                entry = self._parse_entry(block)
                if not updated and entry and entry["ticker"] == ticker and entry["date"] == trade_date and entry["pending"]:
                    fields = block.strip().splitlines()[0].strip()[1:-1].split("|")
                    tag = self._resolved_tag(
                        trade_date, ticker, fields[2].strip(), f"{raw_return:+.1%}",
                        f"{alpha_return:+.1%}", holding_days, resolution_date,
                    )
                    lines = block.strip().splitlines()
                    blocks[index] = f"{tag}\n\n" + "\n".join(lines[1:]).strip() + f"\n\nREFLECTION:\n{reflection}"
                    updated = True
            if not updated:
                return text, None, False
            return self._SEPARATOR.join(self._apply_rotation(blocks)), None, True

        self._mutate(transform)

    def batch_update_with_outcomes(self, updates: list[dict]) -> None:
        if not updates:
            return

        def transform(text):
            update_map = {(u["trade_date"], u["ticker"]): u for u in updates}
            blocks = text.split(self._SEPARATOR)
            changed = False
            for index, block in enumerate(blocks):
                entry = self._parse_entry(block)
                key = (entry["date"], entry["ticker"]) if entry and entry["pending"] else None
                upd = update_map.pop(key, None) if key is not None else None
                if upd is None:
                    continue
                fields = block.strip().splitlines()[0].strip()[1:-1].split("|")
                tag = self._resolved_tag(
                    key[0], key[1], fields[2].strip(), f"{upd['raw_return']:+.1%}",
                    f"{upd['alpha_return']:+.1%}", upd["holding_days"], upd.get("resolution_date"),
                )
                lines = block.strip().splitlines()
                blocks[index] = f"{tag}\n\n" + "\n".join(lines[1:]).strip() + f"\n\nREFLECTION:\n{upd['reflection']}"
                changed = True
            if not changed:
                return text, None, False
            return self._SEPARATOR.join(self._apply_rotation(blocks)), None, True

        self._mutate(transform)

    @staticmethod
    def _resolved_tag(trade_date, ticker, rating, raw_pct, alpha_pct, holding_days,
                      resolution_date, benchmark_name=None) -> str:
        tag = f"[{trade_date} | {ticker} | {rating} | {raw_pct} | {alpha_pct} | {holding_days}d"
        if resolution_date:
            tag += f" | resolved:{resolution_date}"
        if benchmark_name:
            tag += f" | benchmark:{benchmark_name}"
        return tag + "]"

    def _apply_rotation(self, blocks: list[str]) -> list[str]:
        if not self._max_entries or self._max_entries <= 0:
            return blocks
        decisions = []
        for block in blocks:
            stripped = block.strip()
            tag = stripped.splitlines()[0].strip() if stripped else ""
            resolved = tag.startswith("[") and tag.endswith("]") and "| pending]" not in tag
            decisions.append((block, resolved))
        to_drop = max(0, sum(resolved for _, resolved in decisions) - self._max_entries)
        kept = []
        for block, resolved in decisions:
            if resolved and to_drop:
                to_drop -= 1
            else:
                kept.append(block)
        return kept

    def _parse_entry(self, raw: str) -> dict | None:
        lines = raw.strip().splitlines()
        if not lines:
            return None
        tag = lines[0].strip()
        if not (tag.startswith("[") and tag.endswith("]")):
            return None
        fields = [field.strip() for field in tag[1:-1].split("|")]
        if len(fields) < 4:
            return None
        resolved = benchmark = None
        for field in fields[6:]:
            if field.startswith("resolved:"):
                resolved = field[len("resolved:"):].strip()
            elif field.startswith("benchmark:"):
                benchmark = field[len("benchmark:"):].strip()
        body = "\n".join(lines[1:]).strip()
        marker = self._ID_RE.search(body)
        decision_match = self._DECISION_RE.search(body)
        reflection_match = self._REFLECTION_RE.search(body)
        decision = decision_match.group(1).strip() if decision_match else ""
        identity = marker.group(1).strip() if marker else self._decision_id(fields[0], fields[1], fields[2], decision)
        return {
            "date": fields[0], "ticker": fields[1], "rating": fields[2],
            "pending": fields[3] == "pending", "raw": fields[3] if fields[3] != "pending" else None,
            "alpha": fields[4] if len(fields) > 4 else None,
            "holding": fields[5] if len(fields) > 5 else None, "resolved": resolved,
            "decision_id": identity, "legacy_identity": marker is None, "benchmark": benchmark,
            "decision": decision,
            "reflection": reflection_match.group(1).strip() if reflection_match else "",
        }

    def _format_full(self, e: dict) -> str:
        raw, alpha, holding = e["raw"] or "n/a", e["alpha"] or "n/a", e["holding"] or "n/a"
        tag = f"[{e['date']} | {e['ticker']} | {e['rating']} | {raw} | {alpha} | {holding}]"
        parts = [tag, f"DECISION:\n{e['decision']}"]
        if e["reflection"]:
            parts.append(f"REFLECTION:\n{e['reflection']}")
        return "\n\n".join(parts)

    def _format_reflection_only(self, e: dict) -> str:
        tag = f"[{e['date']} | {e['ticker']} | {e['rating']} | {e['raw'] or 'n/a'}]"
        if e["reflection"]:
            return f"{tag}\n{e['reflection']}"
        text = e["decision"][:300]
        return f"{tag}\n{text}{'...' if len(e['decision']) > 300 else ''}"
