#!/usr/bin/env python3
"""Find TF2 VMF parent chains that can set FSOLID_ROOT_PARENT_ALIGNED.

Python 3.10+, standard library only. No Hammer, game installation, or BSP
decompiler is required. This is a conservative SOURCE-map screen: scripts,
templates, instances, server plugins, and runtime I/O can change the result.

    python tf2_teleporter_vmf_scan.py path/to/map.vmf
    python tf2_teleporter_vmf_scan.py path/to/map.vmf --scripts-dir path/to/vscripts
    python tf2_teleporter_vmf_scan.py path/to/map.vmf --json

Exit codes: 0 no static pair or unresolved mutation; 1 a root-aligned pair
was found; 2 review required; 3 read/parse error.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sys


MAX_VMF_BYTES = 128 * 1024 * 1024
MAX_SCRIPT_BYTES = 2 * 1024 * 1024
SPECIAL_NAMES = {"!activator", "!caller", "!self", "!parent", "!player"}
SELF_BSP = re.compile(r"\bself\s*\.\s*SetSolid\s*\(\s*(?:1|SOLID_BSP)\s*\)", re.I)
SCRIPT_MUTATION = re.compile(
    r"\b(?:SetSolid|SetParent|ClearParent|SetSolidFlags|AddSolidFlags|"
    r"RemoveSolidFlags|RunScriptFile|IncludeScript|SpawnEntityFromTable|"
    r"CreateByClassname|AddOutput)\b", re.I)


@dataclass
class Block:
    kind: str
    line: int
    pairs: list[tuple[str, str]] = field(default_factory=list)
    children: list[Block] = field(default_factory=list)

    def get(self, key: str, default: str = "") -> str:
        key = key.casefold()
        for name, value in reversed(self.pairs):
            if name.casefold() == key:
                return value
        return default


@dataclass
class Entity:
    index: int
    block: Block

    def get(self, key: str, default: str = "") -> str:
        return self.block.get(key, default)

    @property
    def classname(self) -> str:
        return self.get("classname").casefold()

    @property
    def name(self) -> str:
        return self.get("targetname") or f"#{self.index}"

    @property
    def identity(self) -> str:
        return f"{self.name} [{self.classname or '?'}; VMF id {self.get('id') or '?'}; line {self.block.line}]"

    def outputs(self) -> list[tuple[str, str]]:
        return [(key, value) for child in self.block.children
                if child.kind.casefold() == "connections"
                for key, value in child.pairs]


class QuotedString(str):
    """Keep quoted braces distinct from VMF block delimiters."""


def decode_text(path: Path, limit: int) -> str:
    raw = path.read_bytes()
    if len(raw) > limit:
        raise ValueError(f"{path.name} exceeds {limit} byte size limit")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


def tokens(text: str):
    """Source KeyValues tokenizer; preserve strings and block line numbers."""
    i, line = 0, 1
    while i < len(text):
        char = text[i]
        if char.isspace():
            if char == "\n":
                line += 1
            i += 1
            continue
        if text.startswith("//", i):
            end = text.find("\n", i + 2)
            i = len(text) if end < 0 else end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise ValueError(f"unterminated comment at line {line}")
            line += text.count("\n", i, end + 2)
            i = end + 2
            continue
        if char in "{}":
            yield char, line
            i += 1
            continue
        start_line = line
        if char == '"':
            i += 1
            value = []
            while i < len(text):
                char = text[i]
                if char == '"':
                    i += 1
                    break
                if char == "\\" and i + 1 < len(text) and text[i + 1] in ('"', "\\"):
                    value.append(text[i + 1])
                    i += 2
                    continue
                if char == "\n":
                    line += 1
                value.append(char)
                i += 1
            else:
                raise ValueError(f"unterminated quoted string at line {start_line}")
            yield QuotedString("".join(value)), start_line
            continue
        start = i
        while i < len(text) and not text[i].isspace() and text[i] not in "{}":
            if text.startswith("//", i) or text.startswith("/*", i):
                break
            i += 1
        if i == start:
            raise ValueError(f"unexpected character {text[i]!r} at line {line}")
        yield text[start:i], start_line


def parse_vmf(text: str) -> Block:
    stream = list(tokens(text))
    pos = 0

    def parse_block(kind: str, line: int) -> Block:
        nonlocal pos
        if (pos >= len(stream) or stream[pos][0] != "{"
                or isinstance(stream[pos][0], QuotedString)):
            raise ValueError(f"expected '{{' after {kind} at line {line}")
        pos += 1
        block = Block(kind, line)
        while pos < len(stream):
            key, key_line = stream[pos]
            pos += 1
            if key == "}" and not isinstance(key, QuotedString):
                return block
            if key == "{" and not isinstance(key, QuotedString):
                raise ValueError(f"unexpected '{{' at line {key_line}")
            if pos >= len(stream):
                raise ValueError(f"value or block missing after {key} at line {key_line}")
            value, _ = stream[pos]
            if value == "{" and not isinstance(value, QuotedString):
                block.children.append(parse_block(key, key_line))
            elif value == "}" and not isinstance(value, QuotedString):
                raise ValueError(f"value missing after {key} at line {key_line}")
            else:
                block.pairs.append((key, value))
                pos += 1
        raise ValueError(f"unterminated {kind} block at line {line}")

    root = Block("root", 1)
    while pos < len(stream):
        kind, line = stream[pos]
        pos += 1
        if kind in ("{", "}") and not isinstance(kind, QuotedString):
            raise ValueError(f"unexpected {kind} at line {line}")
        root.children.append(parse_block(kind, line))
    if not any(block.kind.casefold() == "world" and
               block.get("classname").casefold() == "worldspawn"
               for block in root.children):
        raise ValueError("not a complete VMF: world/worldspawn block missing")
    return root


def squirrel_code_only(source: str) -> str:
    """Blank comments and quoted strings before matching executable calls."""
    result = list(source)
    i = 0
    while i < len(source):
        if source.startswith("//", i):
            end = source.find("\n", i)
            end = len(source) if end < 0 else end
        elif source.startswith("/*", i):
            close = source.find("*/", i + 2)
            end = len(source) if close < 0 else close + 2
        elif source[i] in ('"', "'"):
            quote = source[i]
            end = i + 1
            while end < len(source):
                if source[end] == "\\":
                    end += 2
                    continue
                if source[end] == quote:
                    end += 1
                    break
                end += 1
        else:
            i += 1
            continue
        for position in range(i, min(end, len(source))):
            if result[position] != "\n":
                result[position] = " "
        i = end
    return "".join(result)


def walk(block: Block):
    for child in block.children:
        yield child
        yield from walk(child)


def as_int(value: str) -> int | None:
    try:
        value = value.strip()
        return int(value, 16 if value.casefold().startswith("0x") else 10)
    except ValueError:
        return None


def true_key(value: str) -> bool | None:
    if not value.strip():
        return False
    if value.strip().casefold() in ("true", "yes"):
        return True
    if value.strip().casefold() in ("false", "no"):
        return False
    try:
        return float(value) != 0.0
    except ValueError:
        return None


def output_parts(value: str) -> list[str]:
    return value.split("\x1b") if "\x1b" in value else value.split(",")


def script_names(value: str) -> list[str]:
    return [name.replace("\\", "/").casefold().removesuffix(".nut") + ".nut"
            for name in re.split(r"[\s;]+", value.strip()) if name]


def script_path(name: str, vmf: Path, dirs: list[Path]) -> Path | None:
    clean = name.replace("\\", "/").lstrip("/")
    if ".." in Path(clean).parts:
        return None
    bases = [*dirs, vmf.parent / "scripts" / "vscripts", vmf.parent,
             vmf.parent.parent / "scripts" / "vscripts"]
    for base in bases:
        for candidate in (base / clean, base / Path(clean).name):
            if candidate.is_file():
                return candidate
    return None


# Rules below are from TF2 game/server sources. The scanner reports every
# descendant of a known BSP root, including non-solid children: SetParent
# applies the flag regardless of child solid type. A non-solid child normally
# needs a direct ClipRayToEntity call to enter the engine path.
SOLIDBSP_KEY_CLASSES = {
    "func_rotating", "func_door_rotating", "func_brush", "momentary_rot_button",
    "func_reflective_glass", "func_forcefield", "func_respawnroomvisualizer",
    "func_monitor",
}
ALWAYS_BSP_CLASSES = {
    "func_train", "func_plat", "func_platrot", "func_trackchange",
    "func_trackautochange", "func_button", "func_wall", "func_wall_toggle",
    "func_conveyor", "func_breakable", "func_guntarget", "func_lod",
    "func_vehicleclip", "func_breakable_surf", "color_correction_volume",
    "func_nav_avoid", "func_nav_prefer", "func_tfbot_hint",
    "func_nobuild", "func_respawnroom", "func_capturezone",
    "func_changeclass", "func_flagdetectionzone", "func_achievement",
    "func_passtime_no_ball_zone", "func_regenerate", "func_respawnflag",
    "func_upgradestation", "func_suggested_build", "func_passtime_goal",
    "func_nogrenades", "func_passtime_goalie_zone", "func_flag_alert",
    "func_croc", "func_friction", "func_nav_prerequisite",
    "func_powerupvolume",
}
TRIGGER_BSP_CLASSES = {
    "trigger_brush", "trigger_soundscape", "trigger_capture_area",
    "trigger_remove", "trigger_hurt", "trigger_multiple", "trigger_once",
    "trigger_look", "trigger_transition", "trigger_changelevel",
    "trigger_push", "trigger_teleport", "trigger_teleport_relative",
    "trigger_togglesave", "trigger_autosave", "trigger_gravity",
    "trigger_cdaudio", "trigger_proximity", "trigger_impact",
    "trigger_playermovement", "trigger_serverragdoll",
    "trigger_apply_impulse", "trigger_stun", "trigger_ignite_arrows",
    "trigger_timer_door", "trigger_bot_tag",
    "trigger_add_tf_player_condition", "trigger_player_respawn_override",
    "trigger_ignite", "trigger_particle",
    "trigger_remove_tf_player_condition",
    "trigger_add_or_remove_tf_player_attributes",
    "trigger_passtime_ball", "trigger_catapult",
}
KNOWN_NON_BSP_CLASSES = {
    "func_door", "func_movelinear", "momentary_door", "func_water",
    "func_water_analog",
    "func_rot_button", "func_pushable", "func_weight_button",
    "prop_dynamic", "prop_dynamic_override",
    "prop_physics", "prop_physics_override", "prop_physics_multiplayer",
    "func_physbox", "func_physbox_multiplayer", "path_track", "path_corner",
    "logic_relay", "logic_auto", "logic_script", "info_particle_system",
    "point_template", "env_entity_maker", "ambient_generic",
    "info_teleport_destination", "info_player_teamspawn", "team_control_point",
    "item_teamflag", "func_detail", "func_viscluster", "func_instance",
    "func_instance_parms", "func_occluder",
    "trigger_wind", "trigger_vphysics_motion",
}
NON_SOLID_CHILD_CLASSES = {
    "info_particle_system", "logic_relay", "logic_auto", "logic_script",
    "ambient_generic", "path_track", "path_corner", "point_template",
    "env_entity_maker", "team_control_point", "info_teleport_destination",
    "func_instance", "func_instance_parms",
}


def root_type(ent: Entity, script_bsp_ids: set[int]) -> tuple[str, str]:
    if ent.index in script_bsp_ids:
        return "bsp", "referenced VScript calls self.SetSolid(1/SOLID_BSP)"
    cls = ent.classname
    flags = as_int(ent.get("spawnflags", "0"))
    if cls in ("func_tracktrain", "func_tanktrain"):
        if flags is None:
            return "review", "non-numeric spawnflags; cannot evaluate HL1 Train bit"
        return ("bsp", "HL1 Train spawnflag 128 selects SOLID_BSP") if flags & 128 else (
            "non_bsp", "HL1 Train spawnflag 128 is clear")
    if cls in SOLIDBSP_KEY_CLASSES:
        enabled = true_key(ent.get("solidbsp"))
        if enabled is None:
            return "review", "solidbsp key is not a recognizable boolean"
        return ("bsp", "solidbsp=1 selects SOLID_BSP") if enabled else (
            "non_bsp", "solidbsp is disabled/default 0")
    if cls in ALWAYS_BSP_CLASSES:
        return "bsp", f"{cls} spawns SOLID_BSP"
    if cls in TRIGGER_BSP_CLASSES:
        return "bsp", "TF brush trigger spawns SOLID_BSP when unparented"
    if cls in KNOWN_NON_BSP_CLASSES:
        return "non_bsp", f"{cls} is not a BSP root in the inspected source"
    if cls.startswith("trigger_") or cls == "trigger":
        return "review", f"unclassified trigger class {cls}"
    return "review", f"unclassified root class {cls or '?'}"


def child_traceability(ent: Entity) -> str:
    cls = ent.classname
    flags = as_int(ent.get("spawnflags")) or 0
    if cls in NON_SOLID_CHILD_CLASSES:
        return "not normally traced"
    if cls == "func_brush" and as_int(ent.get("Solidity")) == 1:
        return "not normally traced (func_brush Solidity=1)"
    if cls in ("func_tracktrain", "func_tanktrain") and flags & 8:
        return "not normally traced (Passable flag)"
    if cls in ("func_door", "func_door_rotating") and flags & 8:
        return "not normally traced (Passable flag)"
    if cls == "func_rotating" and flags & 64:
        return "not normally traced (Not Solid flag)"
    if cls in ("prop_dynamic", "prop_dynamic_override") and as_int(
            ent.get("solid", "0")) == 0:
        return "not normally traced (prop solid=0)"
    if cls.startswith("trigger_"):
        return "trigger/mask-dependent"
    return "normally traceable or class-dependent"


def scan(path: Path, script_dirs: list[Path]) -> dict:
    tree = parse_vmf(decode_text(path, MAX_VMF_BYTES))
    entities = [Entity(i, block) for i, block in enumerate(walk(tree))
                if block.kind.casefold() == "entity"]
    by_name: dict[str, list[Entity]] = defaultdict(list)
    for ent in entities:
        if ent.get("targetname"):
            by_name[ent.get("targetname").casefold()].append(ent)

    findings: list[dict] = []
    review: list[dict] = []
    script_bsp_ids: set[int] = set()
    script_references: list[dict] = []
    for ent in entities:
        names = script_names(ent.get("vscripts"))
        for name in names:
            source = script_path(name, path, script_dirs)
            if source is None:
                script_references.append({"entity": ent.identity, "script": name,
                                          "reason": "referenced VScript not found; supply --scripts-dir"})
                continue
            try:
                body = decode_text(source, MAX_SCRIPT_BYTES)
            except (OSError, ValueError) as exc:
                script_references.append({"entity": ent.identity, "script": str(source),
                                          "reason": str(exc)})
                continue
            code = squirrel_code_only(body)
            if SELF_BSP.search(code):
                script_bsp_ids.add(ent.index)
                script_references.append({"entity": ent.identity, "script": str(source),
                                          "reason": "self.SetSolid(1/SOLID_BSP) found"})
            elif SCRIPT_MUTATION.search(code):
                script_references.append({"entity": ent.identity, "script": str(source),
                                          "reason": "collision/parenting mutation; review manually"})
            else:
                script_references.append({"entity": ent.identity, "script": str(source),
                                          "reason": "VScript can create runtime entities/I-O; review manually"})

    def matches_name(name: str) -> list[Entity]:
        lowered = name.casefold()
        if "*" in lowered:
            # Source NamesMatch uses only the prefix before its first '*'.
            prefix = lowered.split("*", 1)[0]
            return [ent for key, values in by_name.items()
                    if key.startswith(prefix) for ent in values]
        return by_name.get(lowered, [])

    def roots(ent: Entity, seen: frozenset[int] = frozenset()) -> tuple[list[Entity], str | None]:
        if ent.index in seen:
            return [], "parent cycle"
        parent = ent.get("parentname").split(",", 1)[0].strip()
        if not parent:
            return [ent], None
        if parent.casefold() in SPECIAL_NAMES or parent.startswith("!"):
            return [], f"dynamic parent token {parent}"
        candidates = matches_name(parent)
        if not candidates:
            return [], f"parent targetname {parent!r} not found in this VMF"
        result: dict[int, Entity] = {}
        errors = []
        for candidate in candidates:
            found, error = roots(candidate, seen | {ent.index})
            result.update((item.index, item) for item in found)
            if error:
                errors.append(error)
        return list(result.values()), "; ".join(sorted(set(errors))) or None

    for child in entities:
        if not child.get("parentname"):
            continue
        root_entities, error = roots(child)
        if error:
            review.append({"type": "unresolved_parent", "child": child.identity,
                           "parentname": child.get("parentname"), "reason": error})
        for root in root_entities:
            kind, reason = root_type(root, script_bsp_ids)
            row = {"type": "static_parent_chain", "root": root.identity,
                   "child": child.identity, "root_reason": reason,
                   "child_trace": child_traceability(child),
                   "parentname": child.get("parentname")}
            if kind == "bsp":
                findings.append(row)
            elif kind == "review":
                review.append(row)

    dynamic_outputs: list[dict] = []
    for source in entities:
        for key, value in source.outputs():
            parts = output_parts(value)
            if len(parts) < 2:
                continue
            target, action = parts[0].strip(), parts[1].strip().casefold()
            parameter = parts[2].strip() if len(parts) > 2 else ""
            if action in ("setparent", "setparentattachment", "setparentattachmentmaintainoffset"):
                if action == "setparent" and parameter:
                    parents = matches_name(parameter)
                    children = matches_name(target)
                    if parents and children:
                        for parent in parents:
                            root_entities, error = roots(parent)
                            if error:
                                review.append({"type": "runtime_setparent", "source": source.identity,
                                               "output": key, "value": value, "reason": error})
                            for root in root_entities:
                                kind, reason = root_type(root, script_bsp_ids)
                                if kind in ("bsp", "review"):
                                    review.append({"type": "runtime_setparent", "source": source.identity,
                                                   "output": key, "value": value,
                                                   "root": root.identity, "root_reason": reason,
                                                   "child": child.identity})
                    else:
                        review.append({"type": "runtime_setparent", "source": source.identity,
                                       "output": key, "value": value,
                                       "reason": "dynamic or unresolved target/parent name"})
                elif action == "setparent":
                    review.append({"type": "runtime_setparent", "source": source.identity,
                                   "output": key, "value": value,
                                   "reason": "empty/dynamic parent; prior root-aligned flag may persist"})
                else:
                    dynamic_outputs.append({"source": source.identity, "output": key,
                                            "value": value, "note": "attachment change without new parent"})
            elif action in ("runscriptfile", "runscriptcode", "callscriptfunction",
                            "setsolid", "addoutput"):
                review.append({"type": "runtime_mutation_output", "source": source.identity,
                               "output": key, "value": value})
            if action in ("setparent", "runscriptfile", "runscriptcode", "callscriptfunction",
                          "setsolid", "addoutput"):
                dynamic_outputs.append({"source": source.identity, "output": key, "value": value})

    for ref in script_references:
        # Even a known self.SetSolid(BSP) can become risky through a child
        # spawned/parented by script after the authored VMF snapshot.
        review.append({"type": "script", **ref})
    for ent in entities:
        if ent.classname == "func_instance":
            review.append({"type": "unexpanded_instance", "entity": ent.identity,
                           "file": ent.get("file"),
                           "reason": "Hammer instance entities are absent until VBSP expands them"})
        if ent.classname == "point_template":
            review.append({"type": "template", "entity": ent.identity,
                           "reason": "template cloning can create runtime parent chains"})
        if ent.get("thinkfunction").strip():
            review.append({"type": "thinkfunction", "entity": ent.identity,
                           "function": ent.get("thinkfunction"),
                           "reason": "script think function can change collision/parenting"})

    # Deduplicate VMF findings caused by duplicate targetname resolution.
    findings = list({json.dumps(row, sort_keys=True): row for row in findings}.values())
    review = list({json.dumps(row, sort_keys=True): row for row in review}.values())
    status = "FOUND" if findings else "REVIEW" if review else "NONE_FOUND"
    return {"vmf": str(path.resolve()), "status": status,
            "entity_count": len(entities), "found_count": len(findings),
            "review_count": len(review), "findings": findings, "review": review,
            "script_references": script_references, "dynamic_outputs": dynamic_outputs}


def show(report: dict) -> None:
    print(f"{Path(report['vmf']).name}: {report['status']} "
          f"({report['found_count']} BSP-root descendants, "
          f"{report['review_count']} review items; {report['entity_count']} entities)")
    for item in report["findings"][:30]:
        print(f"  FOUND {item['root']} -> {item['child']}")
        print(f"        {item['root_reason']}; {item['child_trace']}")
    if report["found_count"] > 30:
        print(f"  ... {report['found_count'] - 30} further pairs (use --json)")
    for item in report["review"][:15]:
        print("  REVIEW " + "; ".join(str(value) for key, value in item.items()
                                     if key not in ("type", "value")))
    if report["review_count"] > 15:
        print(f"  ... {report['review_count'] - 15} further review items (use --json)")
    print("  Static VMF screen only; external scripts/plugins and the compiled BSP may differ.")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("vmfs", nargs="+", type=Path, metavar="MAP.vmf")
    parser.add_argument("--scripts-dir", action="append", type=Path, default=[],
                        help="directory containing referenced .nut scripts (repeatable)")
    parser.add_argument("--json", action="store_true", help="print machine-readable results")
    args = parser.parse_args(argv)
    reports, errors = [], []
    for path in args.vmfs:
        try:
            reports.append(scan(path, args.scripts_dir))
        except (OSError, ValueError, RecursionError) as exc:
            errors.append({"vmf": str(path), "error": str(exc)})
    if args.json:
        print(json.dumps({"results": reports, "errors": errors}, indent=2))
    else:
        for report in reports:
            show(report)
        for error in errors:
            print(f"{error['vmf']}: ERROR {error['error']}", file=sys.stderr)
    if errors:
        return 3
    if any(report["status"] == "FOUND" for report in reports):
        return 1
    if any(report["status"] == "REVIEW" for report in reports):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
