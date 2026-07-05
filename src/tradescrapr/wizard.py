"""Form state machine for the interactive job builder (`tradescrapr new`).

Pure logic — no rich, no terminal. The UI layer (wizard_ui) feeds decoded key
names ("up", "enter", "a", ...) into FormState.handle_key and renders whatever
state results. Keeping this side effect-free makes the whole flow testable
without a TTY.
"""

from __future__ import annotations

import enum
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

    def visible(self) -> list[PickItem]:
        if not self.query:
            return self.items
        q = self.query.lower()
        return [it for it in self.items if q in it.search]

    def handle_key(self, key: str) -> str | None:
        """Returns "done", "cancel", or None (picker stays open)."""
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
# Form state
# ---------------------------------------------------------------------------


class Mode(enum.Enum):
    FORM = "form"
    EDIT_TEXT = "edit_text"
    PICKER = "picker"
    CONFIRM_OVERWRITE = "confirm_overwrite"
    DONE = "done"


class Action(enum.Enum):
    NONE = "none"
    QUIT = "quit"
    SAVE = "save"


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
