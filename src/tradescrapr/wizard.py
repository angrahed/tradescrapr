"""Form state machine for the interactive job builder (`tradescrapr new`).

Pure logic — no rich, no terminal. The UI layer (wizard_ui) feeds decoded key
names ("up", "enter", "a", ...) into FormState.handle_key and renders whatever
state results. Keeping this side effect-free makes the whole flow testable
without a TTY.
"""

from __future__ import annotations

import csv
import enum
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .planner import parse_periods
from .refs import Refs, _partner_code, _partner_iso3

FLOW_CHOICES = ("M", "X", "RM", "RX")
FLOW_LABELS = {"M": "imports", "X": "exports", "RM": "re-imports", "RX": "re-exports"}
CMD_MODES = ("total", "level2", "level4", "level6", "codes")
IMPORTABLE_PICKERS = {"reporters", "partners", "commodities"}
_IMPORT_EXTENSIONS = {".txt", ".csv", ".json"}
_IDENT_SPLIT_RE = re.compile(r"[\s,;]+")
_HEADER_WORDS = {
    "code",
    "codes",
    "country",
    "countries",
    "country_code",
    "id",
    "ids",
    "iso3",
    "partner",
    "partner_code",
    "product",
    "product_code",
    "product_codes",
    "reporter",
    "reporter_code",
    "commodity",
    "commodity_code",
    "hs_code",
    "cmd_code",
    "cmdcode",
}
_JSON_LIST_KEYS = (
    "codes",
    "items",
    "reporters",
    "countries",
    "partners",
    "commodities",
    "products",
    "productcodes",
)
_JSON_VALUE_KEYS = (
    "code",
    "id",
    "iso3",
    "reporter",
    "reportercode",
    "partner",
    "partnercode",
    "commodity",
    "commoditycode",
    "cmdcode",
    "product",
    "productcode",
)


@dataclass(frozen=True)
class FieldDef:
    key: str
    label: str
    kind: str  # text | cycle | bool | picker | commodities
    choices: tuple[str, ...] = ()


FIELDS: tuple[FieldDef, ...] = (
    FieldDef("name", "name", "text"),
    FieldDef("dataset", "dataset", "cycle", ("final", "tariffline")),
    FieldDef("type", "type", "cycle", ("C", "S")),
    FieldDef("freq", "freq", "cycle", ("A", "M")),
    FieldDef("classification", "classification", "text"),
    FieldDef("reporters", "reporters", "picker"),
    FieldDef("partners", "partners", "picker"),
    FieldDef("flows", "flows", "picker"),
    FieldDef("commodities", "commodities", "commodities"),
    FieldDef("periods", "periods", "text"),
    FieldDef("breakdown", "breakdown", "cycle", ("classic", "full")),
    FieldDef("include_desc", "include desc", "bool"),
    FieldDef("use_availability", "use availability", "bool"),
)

_FIELD_INDEX = {f.key: i for i, f in enumerate(FIELDS)}


def default_values() -> dict[str, Any]:
    """Defaults mirror planner.load_spec; reporters and periods must be filled."""
    return {
        "name": "",
        "dataset": "final",
        "type": "C",
        "freq": "A",
        "classification": "HS",
        "reporters": [],  # "all" or list of numeric codes
        "partners": "all",  # "all" or list of numeric codes
        "flows": ["M", "X"],
        "commodities": {"mode": "level6", "codes": []},
        "periods": "",
        "breakdown": "classic",
        "include_desc": False,
        "use_availability": True,
    }


# ---------------------------------------------------------------------------
# Multi-select picker
# ---------------------------------------------------------------------------


@dataclass
class PickItem:
    code: str
    label: str
    search: str  # lowercase haystack for incremental search


@dataclass
class PickerState:
    title: str
    field_key: str
    items: list[PickItem]
    selected: set[str] = field(default_factory=set)
    all_selected: bool = False
    query: str = ""
    cursor: int = 0
    allow_all: bool = True

    def can_import(self) -> bool:
        return self.field_key in IMPORTABLE_PICKERS

    def visible(self) -> list[PickItem]:
        if not self.query:
            return self.items
        q = self.query.lower()
        return [it for it in self.items if q in it.search]

    def handle_key(self, key: str) -> str | None:
        """Returns "done", "cancel", "import", or None (picker stays open)."""
        vis = self.visible()
        if key == "up":
            self.cursor = max(0, self.cursor - 1)
        elif key == "down":
            self.cursor = min(max(0, len(vis) - 1), self.cursor + 1)
        elif key == "space":
            if vis:
                code = vis[min(self.cursor, len(vis) - 1)].code
                if code in self.selected:
                    self.selected.discard(code)
                else:
                    self.selected.add(code)
                self.all_selected = False
        elif key == "*":
            if self.allow_all:
                self.all_selected = not self.all_selected
        elif key == "backspace":
            self.query = self.query[:-1]
            self.cursor = 0
        elif key == "enter":
            return "done"
        elif key == "esc":
            return "cancel"
        elif key == "I" and self.can_import():
            return "import"
        elif len(key) == 1 and key.isprintable():
            self.query += key.lower()
            self.cursor = 0
        return None


def reporter_items(refs: Refs) -> list[PickItem]:
    out = []
    for r in refs.reporters():
        if r.get("isGroup"):
            continue
        code = str(r["reporterCode"])
        iso3 = r.get("reporterCodeIsoAlpha3") or ""
        text = str(r.get("text") or "")
        out.append(
            PickItem(
                code=code,
                label=f"{iso3 or code:<4} {text}".rstrip(),
                search=f"{code} {iso3} {text}".lower(),
            )
        )
    return out


def partner_items(refs: Refs) -> list[PickItem]:
    out = []
    for r in refs.partners():
        code = _partner_code(r)
        if r.get("isGroup") and code != "0":  # keep the World aggregate
            continue
        iso3 = _partner_iso3(r) or ""
        text = str(r.get("text") or "")
        out.append(
            PickItem(
                code=code,
                label=f"{iso3 or code:<4} {text}".rstrip(),
                search=f"{code} {iso3} {text}".lower(),
            )
        )
    return out


def flow_items() -> list[PickItem]:
    return [
        PickItem(code=f, label=f"{f:<4} {FLOW_LABELS[f]}", search=f"{f} {FLOW_LABELS[f]}".lower())
        for f in FLOW_CHOICES
    ]


def commodity_items(refs: Refs, cl_code: str) -> list[PickItem]:
    out = []
    for r in refs.classification(cl_code):
        code = str(r.get("id", ""))
        if not code or code.upper() == "TOTAL":
            continue
        text = str(r.get("text") or "")
        out.append(
            PickItem(
                code=code,
                label=f"{code:<8} {text}".rstrip(),
                search=f"{code} {text}".lower(),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Picker file import
# ---------------------------------------------------------------------------


def _clean_identifier(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (str, int)):
        return None
    s = str(value).strip().strip("\"'")
    return s or None


def _normalise_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _looks_like_header(value: str) -> bool:
    cleaned = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return cleaned in _HEADER_WORDS


def _parse_txt_import(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ConfigError(f"cannot read import file {path}: {e.strerror or e}") from e
    return [tok for tok in (_clean_identifier(t) for t in _IDENT_SPLIT_RE.split(text)) if tok]


def _parse_csv_import(path: Path) -> list[str]:
    values: list[str] = []
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.reader(f):
                for cell in row:
                    ident = _clean_identifier(cell)
                    if ident:
                        values.append(ident)
                        break
    except OSError as e:
        raise ConfigError(f"cannot read import file {path}: {e.strerror or e}") from e
    except csv.Error as e:
        raise ConfigError(f"invalid CSV import file {path}: {e}") from e
    if values and _looks_like_header(values[0]):
        values = values[1:]
    return values


def _json_sequence(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        by_key = {_normalise_key(k): v for k, v in payload.items()}
        for key in _JSON_LIST_KEYS:
            value = by_key.get(key)
            if isinstance(value, list):
                return value
    raise ConfigError("JSON import must be an array or an object containing an array of codes")


def _json_object_identifier(row: dict[str, Any]) -> str | None:
    by_key = {_normalise_key(k): v for k, v in row.items()}
    for key in _JSON_VALUE_KEYS:
        ident = _clean_identifier(by_key.get(key))
        if ident:
            return ident
    return None


def _parse_json_import(path: Path) -> list[str]:
    try:
        with path.open(encoding="utf-8-sig") as f:
            payload = json.load(f)
    except OSError as e:
        raise ConfigError(f"cannot read import file {path}: {e.strerror or e}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"invalid JSON import file {path}: {e}") from e

    values: list[str] = []
    for row in _json_sequence(payload):
        ident = _json_object_identifier(row) if isinstance(row, dict) else _clean_identifier(row)
        if ident:
            values.append(ident)
    return values


def parse_import_file(path: Path) -> list[str]:
    path = path.expanduser()
    ext = path.suffix.lower()
    if ext not in _IMPORT_EXTENSIONS:
        allowed = ", ".join(sorted(_IMPORT_EXTENSIONS))
        raise ConfigError(f"unsupported import file extension {ext!r} (use {allowed})")
    if ext == ".txt":
        values = _parse_txt_import(path)
    elif ext == ".csv":
        values = _parse_csv_import(path)
    else:
        values = _parse_json_import(path)
    if not values:
        raise ConfigError(f"import file {path} contains no identifiers")
    return values


def resolve_imported_selection(picker: PickerState, refs: Refs, identifiers: list[str]) -> set[str]:
    if not picker.can_import():
        raise ConfigError(f"{picker.title} does not support file import")

    valid = {it.code for it in picker.items}
    resolved: list[str] = []
    invalid: list[str] = []
    for ident in identifiers:
        try:
            if picker.field_key == "reporters":
                code = refs.reporter_code(ident)
            elif picker.field_key == "partners":
                code = refs.partner_code(ident)
            else:
                code = str(ident).strip()
        except ConfigError:
            invalid.append(ident)
            continue
        if code in valid:
            resolved.append(code)
        else:
            invalid.append(ident)

    if invalid:
        shown = ", ".join(repr(v) for v in invalid[:5])
        extra = f" +{len(invalid) - 5}" if len(invalid) > 5 else ""
        raise ConfigError(f"import contains unknown {picker.title}: {shown}{extra}")
    if not resolved:
        raise ConfigError(f"import file contains no {picker.title}")
    return set(resolved)


# ---------------------------------------------------------------------------
# Form state
# ---------------------------------------------------------------------------


class Mode(enum.Enum):
    FORM = "form"
    EDIT_TEXT = "edit_text"
    PICKER = "picker"
    IMPORT_PATH = "import_path"
    CONFIRM_OVERWRITE = "confirm_overwrite"
    DONE = "done"


class Action(enum.Enum):
    NONE = "none"
    QUIT = "quit"
    SAVE = "save"
    IMPORT = "import"


@dataclass
class FormState:
    values: dict[str, Any]
    jobs_dir: Path
    cursor: int = 0
    mode: Mode = Mode.FORM
    picker: PickerState | None = None
    edit_buffer: str = ""
    error: str | None = None

    @classmethod
    def initial(cls, jobs_dir: Path) -> FormState:
        return cls(values=default_values(), jobs_dir=jobs_dir)

    def current_field(self) -> FieldDef:
        return FIELDS[self.cursor]

    # -- validation ---------------------------------------------------------

    def validate_field(self, key: str) -> str | None:
        v = self.values[key]
        if key == "name":
            name = str(v).strip()
            if not name:
                return "name is required"
            if any(c in name for c in " /\\"):
                return "name must be a slug without spaces or slashes"
        elif key == "classification":
            if not str(v).strip():
                return "classification is required"
        elif key == "reporters":
            if v != "all" and not v:
                return "select at least one reporter (or * for all)"
        elif key == "flows":
            if not v:
                return "select at least one flow"
        elif key == "commodities":
            if v["mode"] == "codes" and not v["codes"]:
                return "pick at least one commodity code"
        elif key == "periods":
            if not str(v).strip():
                return "periods is required (e.g. 2015:2024)"
            try:
                parse_periods(str(v), self.values["freq"])
            except ConfigError as e:
                return str(e)
        return None

    def validate_all(self) -> tuple[int, str] | None:
        """(field index, message) of the first invalid field, or None if all good."""
        for f in FIELDS:
            msg = self.validate_field(f.key)
            if msg:
                return _FIELD_INDEX[f.key], msg
        return None

    # -- key handling ---------------------------------------------------------

    def handle_key(self, key: str, make_picker: Callable[[str], PickerState | None]) -> Action:
        if self.mode is Mode.FORM:
            return self._handle_form_key(key, make_picker)
        if self.mode is Mode.EDIT_TEXT:
            self._handle_edit_key(key)
            return Action.NONE
        if self.mode is Mode.PICKER:
            self._handle_picker_key(key)
            return Action.NONE
        if self.mode is Mode.IMPORT_PATH:
            return self._handle_import_key(key)
        if self.mode is Mode.CONFIRM_OVERWRITE:
            self.mode = Mode.FORM
            return Action.SAVE if key in ("y", "Y") else Action.NONE
        return Action.NONE  # DONE handled by the UI layer

    def _handle_form_key(
        self, key: str, make_picker: Callable[[str], PickerState | None]
    ) -> Action:
        self.error = None
        fdef = self.current_field()
        if key == "up":
            self.cursor = (self.cursor - 1) % len(FIELDS)
        elif key == "down":
            self.cursor = (self.cursor + 1) % len(FIELDS)
        elif key in ("q", "Q"):
            return Action.QUIT
        elif key in ("s", "S"):
            bad = self.validate_all()
            if bad:
                self.cursor, self.error = bad
                return Action.NONE
            if (self.jobs_dir / f"{self.values['name'].strip()}.yaml").exists():
                self.mode = Mode.CONFIRM_OVERWRITE
                return Action.NONE
            return Action.SAVE
        elif key == "enter":
            self._activate(fdef, make_picker)
        elif key in ("left", "right"):
            if fdef.kind == "cycle":
                self._cycle(fdef, -1 if key == "left" else 1)
            elif fdef.kind == "bool":
                self.values[fdef.key] = not self.values[fdef.key]
            elif fdef.kind == "commodities":
                self._cycle_cmd_mode(-1 if key == "left" else 1)
        elif key == "space" and fdef.kind == "bool":
            self.values[fdef.key] = not self.values[fdef.key]
        return Action.NONE

    def _activate(self, fdef: FieldDef, make_picker: Callable[[str], PickerState | None]) -> None:
        if fdef.kind == "text":
            self.edit_buffer = str(self.values[fdef.key])
            self.mode = Mode.EDIT_TEXT
        elif fdef.kind == "cycle":
            self._cycle(fdef, 1)
        elif fdef.kind == "bool":
            self.values[fdef.key] = not self.values[fdef.key]
        elif fdef.kind == "picker":
            self._open_picker(fdef.key, make_picker)
        elif fdef.kind == "commodities":
            if self.values["commodities"]["mode"] == "codes":
                self._open_picker("commodities", make_picker)
            else:
                self._cycle_cmd_mode(1)

    def _open_picker(self, key: str, make_picker: Callable[[str], PickerState | None]) -> None:
        picker = make_picker(key)
        if picker is None:
            self.error = "reference data unavailable — run `tradescrapr refs refresh`"
            return
        self.picker = picker
        self.mode = Mode.PICKER

    def _cycle(self, fdef: FieldDef, step: int) -> None:
        cur = fdef.choices.index(self.values[fdef.key])
        self.values[fdef.key] = fdef.choices[(cur + step) % len(fdef.choices)]
        if fdef.key == "freq" and str(self.values["periods"]).strip():
            self.error = self.validate_field("periods")

    def _cycle_cmd_mode(self, step: int) -> None:
        cmd = self.values["commodities"]
        cur = CMD_MODES.index(cmd["mode"])
        cmd["mode"] = CMD_MODES[(cur + step) % len(CMD_MODES)]

    def _handle_edit_key(self, key: str) -> None:
        if key == "esc":
            self.mode = Mode.FORM
            self.error = None
        elif key == "enter":
            self._commit_edit()
        elif key == "backspace":
            self.edit_buffer = self.edit_buffer[:-1]
        elif key == "space":
            self.edit_buffer += " "
        elif len(key) == 1 and key.isprintable():
            self.edit_buffer += key

    def _commit_edit(self) -> None:
        key = self.current_field().key
        old = self.values[key]
        self.values[key] = self.edit_buffer.strip()
        msg = self.validate_field(key)
        if msg:
            self.values[key] = old
            self.error = msg
            return  # stay in EDIT_TEXT so the user can fix the value
        if key == "classification" and self.values[key] != old:
            self.values["commodities"]["codes"] = []
        self.mode = Mode.FORM
        self.error = None

    def _handle_picker_key(self, key: str) -> None:
        assert self.picker is not None
        outcome = self.picker.handle_key(key)
        if outcome == "cancel":
            self.picker = None
            self.mode = Mode.FORM
            self.error = None
        elif outcome == "done":
            self._commit_picker()
        elif outcome == "import":
            self.edit_buffer = ""
            self.mode = Mode.IMPORT_PATH
            self.error = None

    def _handle_import_key(self, key: str) -> Action:
        if key == "esc":
            self.mode = Mode.PICKER
            self.edit_buffer = ""
            self.error = None
        elif key == "enter":
            return Action.IMPORT
        elif key == "backspace":
            self.edit_buffer = self.edit_buffer[:-1]
            self.error = None
        elif key == "space":
            self.edit_buffer += " "
            self.error = None
        elif len(key) == 1 and key.isprintable():
            self.edit_buffer += key
            self.error = None
        return Action.NONE

    def import_picker_file(self, path: Path, refs: Refs) -> None:
        p = self.picker
        if p is None:
            raise ConfigError("no picker is open")
        selected = resolve_imported_selection(p, refs, parse_import_file(path))
        p.selected = selected
        p.all_selected = False
        p.query = ""
        p.cursor = 0
        if p.field_key == "commodities":
            self.values["commodities"]["mode"] = "codes"
        self.edit_buffer = ""
        self.mode = Mode.PICKER
        self.error = None

    def _commit_picker(self) -> None:
        p = self.picker
        assert p is not None
        if p.field_key == "reporters":
            if p.all_selected:
                self.values["reporters"] = "all"
            elif p.selected:
                self.values["reporters"] = sorted(p.selected, key=int)
            else:
                self.error = "select at least one reporter, or * for all"
                return
        elif p.field_key == "partners":
            if p.all_selected or not p.selected:
                self.values["partners"] = "all"
            else:
                self.values["partners"] = sorted(p.selected, key=int)
        elif p.field_key == "flows":
            if not p.selected:
                self.error = "select at least one flow"
                return
            self.values["flows"] = [f for f in FLOW_CHOICES if f in p.selected]
        elif p.field_key == "commodities":
            if not p.selected:
                self.error = "pick at least one commodity code"
                return
            self.values["commodities"]["codes"] = sorted(p.selected)
        self.picker = None
        self.mode = Mode.FORM
        self.error = None


# ---------------------------------------------------------------------------
# YAML emission + estimate
# ---------------------------------------------------------------------------


def partner_label(refs: Refs, code: str) -> str:
    for r in refs.partners():
        if _partner_code(r) == str(code):
            return _partner_iso3(r) or str(code)
    return str(code)


def spec_dict(values: dict[str, Any], refs: Refs) -> dict[str, Any]:
    """The YAML job-spec mapping, in a fixed key order so identical selections
    always serialize identically (JobSpec.job_id hashes the raw mapping)."""
    reporters = values["reporters"]
    partners = values["partners"]
    d: dict[str, Any] = {
        "name": str(values["name"]).strip(),
        "dataset": values["dataset"],
        "type": values["type"],
        "freq": values["freq"],
        "classification": values["classification"],
        "reporters": (
            "all"
            if reporters == "all"
            else [refs.reporter_iso3(c) or str(c) for c in sorted(reporters, key=int)]
        ),
        "partners": (
            "all"
            if partners == "all"
            else [partner_label(refs, c) for c in sorted(partners, key=int)]
        ),
        "flows": list(values["flows"]),
    }
    cmd = values["commodities"]
    if cmd["mode"] == "codes":
        d["commodities"] = {"codes": list(cmd["codes"])}
    elif cmd["mode"] != "total":
        d["commodities"] = {"level": int(cmd["mode"].removeprefix("level"))}
    d["periods"] = str(values["periods"]).strip()
    d["breakdown"] = values["breakdown"]
    d["include_desc"] = bool(values["include_desc"])
    d["use_availability"] = bool(values["use_availability"])
    return d


def dump_spec_yaml(values: dict[str, Any], refs: Refs) -> str:
    body = yaml.safe_dump(spec_dict(values, refs), sort_keys=False, default_flow_style=None)
    return f"# generated by tradescrapr new\n{body}"


@dataclass(frozen=True)
class Estimate:
    tasks: int | None = None
    days: int | None = None  # None with tasks set = unlimited budget
    n_reporters: int = 0
    n_periods: int = 0
    n_flow_groups: int = 0
    error: str | None = None


def estimate(
    values: dict[str, Any], refs: Refs, *, combine_flows: bool, daily_budget: int | None
) -> Estimate:
    """Cheap offline task-count estimate mirroring planner.expand's cartesian
    (before the availability filter, which only ever shrinks it)."""
    if not str(values["periods"]).strip():
        return Estimate(error="set periods to estimate")
    try:
        periods = parse_periods(str(values["periods"]), values["freq"])
    except ConfigError as e:
        return Estimate(error=str(e))
    reporters = values["reporters"]
    n_rep = len(refs.all_reporter_codes()) if reporters == "all" else len(reporters)
    if not n_rep:
        return Estimate(error="select reporters to estimate")
    n_groups = 1 if combine_flows else max(1, len(values["flows"]))
    tasks = len(periods) * n_rep * n_groups
    days = max(1, round(tasks / daily_budget)) if daily_budget else None
    return Estimate(
        tasks=tasks,
        days=days,
        n_reporters=n_rep,
        n_periods=len(periods),
        n_flow_groups=n_groups,
    )
