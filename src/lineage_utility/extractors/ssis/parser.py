"""
SSIS lineage parser.
Step 4a / Step 6: Parse a .dtsx package and extract TABLE- and COLUMN-level lineage.

Usage:
    python parse_dtsx.py [path-to-Package.dtsx]

Output:
    - Prints a human-readable summary (table- and column-level)
    - Writes lineage_model.json next to this script

Column-level lineage is derived generically by walking the SSIS `lineageId`
graph inside the data flow:
  * Every component output column has a unique lineageId.
  * A downstream input column's lineageId points at the upstream output column
    that feeds it (identical string) -> this is how we chain components.
  * Derived Column outputs carry a FriendlyExpression; the source columns are the
    component's input columns referenced by that expression.
So we can resolve each destination column back to its true source column(s),
through any number of pass-through/derived hops.
"""
import hashlib
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

from ...domain.legacy import write_legacy_model

SRC_CLASS = "Microsoft.OLEDBSource"
DST_CLASS = "Microsoft.OLEDBDestination"
DERIVED_CLASS = "Microsoft.DerivedColumn"
DATACONVERT_CLASS = "Microsoft.DataConvert"
UNIONALL_CLASS = "Microsoft.UnionAll"
LOOKUP_CLASS = "Microsoft.Lookup"
AGGREGATE_CLASS = "Microsoft.Aggregate"
SORT_CLASS = "Microsoft.Sort"

# Component classes that introduce brand-new lineageIds we must resolve backwards.
TRANSFORM_CLASSES = {
    DERIVED_CLASS, DATACONVERT_CLASS, UNIONALL_CLASS,
    LOOKUP_CLASS, AGGREGATE_CLASS, SORT_CLASS,
}


# --------------------------------------------------------------------------- #
# XML helpers
# --------------------------------------------------------------------------- #
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _attr(elem, name: str):
    for k, v in elem.attrib.items():
        if _local(k) == name:
            return v
    return None


def _bracket(text: str) -> str:
    """'...Columns[FullName]' -> 'FullName' (last [...] group)."""
    m = re.findall(r"\[([^\]]+)\]", text or "")
    return m[-1] if m else (text or "")


def _strip_id_token(text: str):
    """'#{<lineageId>}' -> '<lineageId>'; plain value returned as-is."""
    if text is None:
        return None
    s = text.strip()
    m = re.match(r"^#\{(.*)\}$", s, re.S)
    return (m.group(1) if m else s) or None


def _data_flow_of(ref_id: str) -> str:
    """Return the immediate parent data-flow name for a component refId."""
    parts = [p for p in (ref_id or "").split("\\") if p]
    return parts[-2] if len(parts) >= 2 else (ref_id or "")


def _data_flow_ref(ref_id: str) -> str:
    """Return the full parent data-flow refId, preserving nested containers."""
    parts = [p for p in (ref_id or "").split("\\") if p]
    return "\\".join(parts[:-1]) if len(parts) >= 2 else (ref_id or "")


def _project_metadata(package_path: str) -> dict:
    """Read the stable project ID/name from a sibling .dtproj when available."""
    package = Path(package_path).resolve()
    for project_file in sorted(package.parent.glob("*.dtproj")):
        try:
            project_root = ET.parse(project_file).getroot()
        except (ET.ParseError, OSError):
            continue
        for project in project_root.iter():
            if project is project_root or _local(project.tag) != "Project":
                continue
            props = {}
            for child in project:
                if _local(child.tag) != "Properties":
                    continue
                for prop in child:
                    if _local(prop.tag) != "Property":
                        continue
                    name = _attr(prop, "Name")
                    if name in ("ID", "Name"):
                        props[name] = (prop.text or "").strip()
                break
            if props.get("ID"):
                return {
                    "id": props["ID"].strip("{}").lower(),
                    "name": props.get("Name") or project_file.stem,
                    "file": str(project_file),
                }

    parent_key = str(package.parent).casefold().encode("utf-8")
    return {
        "id": "path-" + hashlib.sha256(parent_key).hexdigest()[:16],
        "name": package.parent.name,
        "file": None,
    }


def _package_qualified_name(package_path: str, project: dict) -> str:
    package_file = quote(Path(package_path).name.casefold(), safe="")
    return f"poc-ssis://project/{project['id']}/package/{package_file}"


# --------------------------------------------------------------------------- #
# Connection managers + table parsing (table-level)
# --------------------------------------------------------------------------- #
def parse_connection_string(conn: str) -> dict:
    server = re.search(r"Data Source=([^;]+)", conn, re.I)
    catalog = re.search(r"Initial Catalog=([^;]+)", conn, re.I)
    return {
        "server": server.group(1).strip() if server else None,
        "database": catalog.group(1).strip() if catalog else None,
    }


def build_connection_managers(root) -> dict:
    managers = {}
    for elem in root.iter():
        if _local(elem.tag) != "ConnectionManager":
            continue
        name = _attr(elem, "ObjectName")
        if not name:
            continue
        conn_str = None
        for sub in elem.iter():
            cs = _attr(sub, "ConnectionString")
            if cs and "Data Source=" in cs:
                conn_str = cs
                break
        if conn_str:
            managers[name] = parse_connection_string(conn_str)
    return managers


def cm_key_from_ref(ref: str) -> str:
    m = re.search(r"\[(.+)\]", ref or "")
    return m.group(1) if m else ref


def split_table(open_rowset: str) -> dict:
    parts = re.findall(r"\[([^\]]+)\]|(\w+)", open_rowset or "")
    tokens = [a or b for a, b in parts if (a or b)]
    if len(tokens) >= 2:
        return {"schema": tokens[-2], "table": tokens[-1]}
    if tokens:
        return {"schema": "dbo", "table": tokens[-1]}
    return {"schema": None, "table": None}


def qualified_name(server: str, database: str, schema: str, table: str) -> str:
    srv = (server or "unknown").replace("\\", ".")
    return f"mssql://{srv}/{database}/{schema}/{table}".lower()


def lookup_reference_qn(comp, managers: dict):
    """Resolve a Lookup component's reference table to a qualifiedName.

    The reference table is named in the Lookup's ``SqlCommand`` (e.g.
    ``SELECT a, b FROM dbo.DimCustomer``) and lives in the DB pointed at by the
    component's ``connection``.
    """
    sql, cm_ref = None, None
    for sub in comp.iter():
        lname = _local(sub.tag)
        if lname == "property" and _attr(sub, "name") in ("SqlCommand", "SqlCommandParam"):
            sql = sql or (sub.text or "")
        elif lname == "connection":
            ref = _attr(sub, "connectionManagerID") or _attr(sub, "connectionManagerRefId")
            if ref:
                cm_ref = cm_key_from_ref(ref)
    if not sql:
        return None
    m = re.search(r"\bFROM\s+([\[\]\w\.]+)", sql, re.I)
    if not m:
        return None
    tbl = split_table(m.group(1))
    conn = managers.get(cm_ref, {})
    return qualified_name(conn.get("server"), conn.get("database"),
                          tbl["schema"], tbl["table"])


def _table_columns(comp) -> list:
    """External metadata column names = the actual DB table columns touched."""
    cols = []
    for sub in comp.iter():
        if _local(sub.tag) == "externalMetadataColumn":
            nm = _attr(sub, "name")
            if nm and nm not in cols:
                cols.append(nm)
    return cols


def parse_component(comp, managers: dict) -> dict:
    name = _attr(comp, "name")
    class_id = _attr(comp, "componentClassID")
    ref_id = _attr(comp, "refId")

    open_rowset, cm_ref, friendly_exprs = None, None, []
    for sub in comp.iter():
        lname = _local(sub.tag)
        if lname == "property" and _attr(sub, "name") == "OpenRowset":
            open_rowset = (sub.text or "").strip()
        elif lname == "property" and _attr(sub, "name") == "FriendlyExpression":
            txt = (sub.text or "").strip()
            if txt:
                friendly_exprs.append(txt)
        elif lname == "connection":
            ref = _attr(sub, "connectionManagerID") or _attr(sub, "connectionManagerRefId")
            if ref:
                cm_ref = cm_key_from_ref(ref)

    info = {"component_name": name, "class_id": class_id, "component_ref": ref_id}
    if open_rowset:
        tbl = split_table(open_rowset)
        conn = managers.get(cm_ref, {})
        info.update({
            "server": conn.get("server"),
            "database": conn.get("database"),
            "schema": tbl["schema"],
            "table": tbl["table"],
            "qualified_name": qualified_name(
                conn.get("server"), conn.get("database"), tbl["schema"], tbl["table"]
            ),
            "columns": _table_columns(comp),
        })
    if friendly_exprs:
        info["expressions"] = friendly_exprs
    if class_id == LOOKUP_CLASS:
        ref_qn = lookup_reference_qn(comp, managers)
        if ref_qn:
            info["reference_qualified_name"] = ref_qn
            info["reference_columns"] = _table_columns(comp)
    return info


# --------------------------------------------------------------------------- #
# Column-level lineage graph
# --------------------------------------------------------------------------- #
def _is_error_output(output_name: str) -> bool:
    return "error output" in (output_name or "").lower()


def _classify(cls: str) -> str:
    return {
        SRC_CLASS: "source",
        DERIVED_CLASS: "derived",
        DATACONVERT_CLASS: "data_conversion",
        UNIONALL_CLASS: "union_all",
        LOOKUP_CLASS: "lookup",
        AGGREGATE_CLASS: "aggregate",
        SORT_CLASS: "sort",
    }.get(cls, "other")


def build_column_graph(root, managers: dict) -> dict:
    """Walk every data-flow component and build the lineageId graph.

    Returns a context dict:
      output_index:        lineageId -> {comp_ref, col_name, kind, expr, props}
      derived_inputs:      derived comp_ref -> [(cachedName, upstream_lineageId)]
      comp_table_qn:       comp_ref -> table qualifiedName (sources & destinations)
      lookup_ref_qn:       lookup comp_ref -> reference-table qualifiedName
      union_out_to_inputs: union output lineageId -> [upstream input lineageIds]
      dest_inputs:         [ {comp_ref, data_flow, table_qn, dest_col, upstream_lid} ]
    """
    output_index, derived_inputs, comp_table_qn = {}, {}, {}
    lookup_ref_qn, union_out_to_inputs, dest_inputs = {}, {}, []

    for comp in root.iter():
        if _local(comp.tag) != "component":
            continue
        cls = _attr(comp, "componentClassID")
        ref = _attr(comp, "refId")
        parsed = parse_component(comp, managers)
        qn = parsed.get("qualified_name")
        kind = _classify(cls)

        if cls in (SRC_CLASS, DST_CLASS) and qn:
            comp_table_qn[ref] = qn
        if cls == LOOKUP_CLASS:
            lookup_ref_qn[ref] = lookup_reference_qn(comp, managers)

        # Index this component's output columns (skip error outputs)
        for out in comp.iter():
            if _local(out.tag) != "output":
                continue
            if _is_error_output(_attr(out, "name")):
                continue
            for oc in out.iter():
                if _local(oc.tag) != "outputColumn":
                    continue
                lid = _attr(oc, "lineageId")
                if not lid:
                    continue
                props = {}
                for pr in oc.iter():
                    if _local(pr.tag) == "property":
                        props[_attr(pr, "name")] = (pr.text or "").strip()
                output_index[lid] = {
                    "comp_ref": ref, "col_name": _attr(oc, "name"),
                    "kind": kind, "expr": props.get("FriendlyExpression"),
                    "props": props,
                }

        # Derived Column inputs (the columns it reads)
        if cls == DERIVED_CLASS:
            derived_inputs[ref] = [
                (_attr(inp, "cachedName"), _attr(inp, "lineageId"))
                for inp in comp.iter() if _local(inp.tag) == "inputColumn"
            ]

        # Union All: input-side mapping (upstream input col -> union output col)
        if cls == UNIONALL_CLASS:
            for inp in comp.iter():
                if _local(inp.tag) != "inputColumn":
                    continue
                up = _attr(inp, "lineageId")
                out_lid = None
                for pr in inp.iter():
                    if _local(pr.tag) == "property" and _attr(pr, "name") == "OutputColumnLineageID":
                        out_lid = _strip_id_token(pr.text)
                if out_lid and up:
                    union_out_to_inputs.setdefault(out_lid, []).append(up)

        # Destination inputs: dest table column <- upstream output column
        if cls == DST_CLASS and qn:
            for inp in comp.iter():
                if _local(inp.tag) != "inputColumn":
                    continue
                ext = _attr(inp, "externalMetadataColumnId")
                dest_col = _bracket(ext) if ext else _attr(inp, "cachedName")
                up = _attr(inp, "lineageId")
                if dest_col and up:
                    dest_inputs.append({
                        "comp_ref": ref, "data_flow": _data_flow_of(ref),
                        "data_flow_ref": _data_flow_ref(ref),
                        "table_qn": qn, "dest_col": dest_col, "upstream_lid": up,
                    })

    return {
        "output_index": output_index,
        "derived_inputs": derived_inputs,
        "comp_table_qn": comp_table_qn,
        "lookup_ref_qn": lookup_ref_qn,
        "union_out_to_inputs": union_out_to_inputs,
        "dest_inputs": dest_inputs,
    }


def resolve_sources(lid, ctx, _seen=None):
    """Resolve an output-column lineageId back to a list of (table_qn, column)."""
    _seen = _seen or set()
    if lid in _seen:
        return []
    _seen.add(lid)

    node = ctx["output_index"].get(lid)
    if node is None:
        return []
    kind = node["kind"]

    if kind == "source":
        qn = ctx["comp_table_qn"].get(node["comp_ref"])
        return [(qn, node["col_name"])] if qn else []

    if kind == "derived":
        reads = ctx["derived_inputs"].get(node["comp_ref"], [])
        expr = node.get("expr") or ""
        # Prefer inputs named in this output column's expression; else all inputs.
        chosen = [up for (cn, up) in reads
                  if cn and re.search(r"\b" + re.escape(cn) + r"\b", expr)]
        if not chosen:
            chosen = [up for (_cn, up) in reads]
        roots = []
        for up in chosen:
            roots.extend(resolve_sources(up, ctx, _seen))
        return roots

    if kind == "data_conversion":
        up = _strip_id_token(node["props"].get("SourceInputColumnLineageID"))
        return resolve_sources(up, ctx, _seen) if up else []

    if kind == "union_all":
        roots = []
        for up in ctx["union_out_to_inputs"].get(lid, []):
            roots.extend(resolve_sources(up, ctx, _seen))
        return roots

    if kind == "lookup":
        # Columns copied from the reference table are ROOTS at that table.
        copy = node["props"].get("CopyFromReferenceColumn")
        if copy:
            qn = ctx["lookup_ref_qn"].get(node["comp_ref"])
            return [(qn, copy)] if qn else []
        return []

    if kind in ("aggregate", "sort"):
        prop = "AggregationColumnId" if kind == "aggregate" else "SortColumnId"
        up = _strip_id_token(node["props"].get(prop))
        return resolve_sources(up, ctx, _seen) if up else []

    return []


def _via_label(node: dict) -> str:
    """Human-readable description of the last hop feeding a destination column."""
    if not node:
        return "passthrough"
    kind = node.get("kind")
    if kind == "derived":
        return f'{node.get("col_name")} = {node.get("expr")}'
    if kind == "data_conversion":
        return "data conversion"
    if kind == "union_all":
        return "union all"
    if kind == "lookup":
        copy = node.get("props", {}).get("CopyFromReferenceColumn")
        return f"lookup ({copy})" if copy else "lookup"
    if kind == "aggregate":
        return "aggregate"
    if kind == "sort":
        return "sort"
    return "passthrough"


def extract_column_mappings(root, managers):
    ctx = build_column_graph(root, managers)
    mappings = []
    for di in ctx["dest_inputs"]:
        up_node = ctx["output_index"].get(di["upstream_lid"], {})
        via = _via_label(up_node)
        roots = resolve_sources(di["upstream_lid"], ctx)
        for (src_qn, src_col) in roots:
            if not src_qn:
                continue
            mappings.append({
                "source_qn": src_qn,
                "source_column": src_col,
                "sink_qn": di["table_qn"],
                "sink_column": di["dest_col"],
                "via": via,
                "data_flow": di["data_flow"],
                "data_flow_ref": di["data_flow_ref"],
            })
    return mappings


def build_tables_index(column_mappings: list, components=()) -> dict:
    """Collect every table referenced by lineage (sources, sinks, lookup refs)
    with the set of columns that actually participate."""
    tables = {}

    def _touch(qn, col):
        if not qn:
            return
        t = tables.setdefault(qn, {"qualified_name": qn, "columns": []})
        m = re.match(r"mssql://[^/]+/([^/]+)/([^/]+)/([^/]+)$", qn or "")
        if m:
            t["database"], t["schema"], t["table"] = m.group(1), m.group(2), m.group(3)
        if col and col not in t["columns"]:
            t["columns"].append(col)

    # Keep table-level lineage even when an unsupported transform prevents
    # end-to-end column resolution.
    for comp in components:
        qn = comp.get("qualified_name")
        _touch(qn, None)
        for col in comp.get("columns", []):
            _touch(qn, col)

        ref_qn = comp.get("reference_qualified_name")
        _touch(ref_qn, None)
        for col in comp.get("reference_columns", []):
            _touch(ref_qn, col)

    for cm in column_mappings:
        _touch(cm["source_qn"], cm["source_column"])
        _touch(cm["sink_qn"], cm["sink_column"])
    return tables


# --------------------------------------------------------------------------- #
# Top-level parse
# --------------------------------------------------------------------------- #
def parse_dtsx(path: str) -> dict:
    tree = ET.parse(path)
    root = tree.getroot()

    package_name = _attr(root, "ObjectName") or Path(path).stem
    managers = build_connection_managers(root)

    sources, destinations, transforms = [], [], []
    for comp in root.iter():
        if _local(comp.tag) != "component":
            continue
        parsed = parse_component(comp, managers)
        cid = parsed.get("class_id")
        ref = parsed.get("component_ref")
        parsed["data_flow"] = _data_flow_of(ref)
        parsed["data_flow_ref"] = _data_flow_ref(ref)
        if cid == SRC_CLASS:
            sources.append(parsed)
        elif cid == DST_CLASS:
            destinations.append(parsed)
        elif cid in TRANSFORM_CLASSES:
            transforms.append(parsed)

    column_mappings = extract_column_mappings(root, managers)
    tables = build_tables_index(
        column_mappings, sources + transforms + destinations)

    # Group discovered tables and mappings into data-flow units. Sources,
    # destinations, and lookup references preserve table-level lineage even
    # when column resolution cannot cross a transform.
    data_flows = {}
    def _flow(name, ref):
        key = ref or name
        return data_flows.setdefault(key, {
            "name": name, "ref_id": ref,
            "source_qns": [], "sink_qns": [],
        })

    def _add_table(comp, side, qn):
        if not qn:
            return
        df = _flow(comp.get("data_flow"), comp.get("data_flow_ref"))
        if qn not in df[side]:
            df[side].append(qn)

    for source in sources:
        _add_table(source, "source_qns", source.get("qualified_name"))
    for transform in transforms:
        _add_table(transform, "source_qns",
                   transform.get("reference_qualified_name"))
    for destination in destinations:
        _add_table(destination, "sink_qns",
                   destination.get("qualified_name"))
    for cm in column_mappings:
        df = _flow(cm["data_flow"], cm.get("data_flow_ref"))
        if cm["source_qn"] not in df["source_qns"]:
            df["source_qns"].append(cm["source_qn"])
        if cm["sink_qn"] not in df["sink_qns"]:
            df["sink_qns"].append(cm["sink_qn"])

    project = _project_metadata(path)
    return {
        "package_name": package_name,
        "package_file": str(path),
        "package_qualified_name": _package_qualified_name(path, project),
        "project": project,
        "connection_managers": managers,
        "sources": sources,
        "transforms": transforms,
        "destinations": destinations,
        "tables": tables,
        "data_flows": list(data_flows.values()),
        "column_mappings": column_mappings,
    }


def print_summary(model: dict) -> None:
    print("=" * 64)
    print(f"  Package: {model['package_name']}")
    print("=" * 64)
    print("\nSOURCES:")
    for s in model["sources"]:
        print(f"  - {s.get('database')}.{s.get('schema')}.{s.get('table')}")
        print(f"      qualifiedName: {s.get('qualified_name')}")
        print(f"      columns: {', '.join(s.get('columns', []))}")
    print("\nTRANSFORMS:")
    for t in model["transforms"]:
        label = t.get("class_id", "").replace("Microsoft.", "")
        print(f"  - [{label}] {t.get('component_name')}  ({t.get('data_flow')})")
        for e in t.get("expressions", []):
            print(f"      expr: {e}")
    print("\nDESTINATIONS:")
    for d in model["destinations"]:
        print(f"  - {d.get('database')}.{d.get('schema')}.{d.get('table')}")
        print(f"      qualifiedName: {d.get('qualified_name')}")
        print(f"      columns: {', '.join(d.get('columns', []))}")

    print("\nTABLES INVOLVED (from lineage):")
    for qn, t in model["tables"].items():
        print(f"  - {t.get('database')}.{t.get('schema')}.{t.get('table')}"
              f"  [{', '.join(t.get('columns', []))}]")

    print("\nCOLUMN-LEVEL LINEAGE (by data flow):")
    for df in model["data_flows"]:
        print(f"\n  == {df['name']} ==")
        for m in model["column_mappings"]:
            if df.get("ref_id"):
                same_flow = m.get("data_flow_ref") == df["ref_id"]
            else:
                same_flow = m["data_flow"] == df["name"]
            if not same_flow:
                continue
            src_t = m["source_qn"].rsplit("/", 1)[-1]
            snk_t = m["sink_qn"].rsplit("/", 1)[-1]
            tag = "" if m["via"] == "passthrough" else f"   [{m['via']}]"
            print(f"    {src_t}.{m['source_column']:<14} -> "
                  f"{snk_t}.{m['sink_column']:<14}{tag}")


def _write_model(model: dict, out_path: Path) -> None:
    write_legacy_model(model, out_path)


def _clear_generated_models(models_dir: Path) -> int:
    """Remove previous batch outputs so deleted packages stay deleted."""
    removed = 0
    for old in models_dir.glob("*.json"):
        old.unlink()
        removed += 1
    for temp in models_dir.glob("*.json.tmp"):
        temp.unlink()
    return removed


def _iter_dtsx(path: Path):
    """Return the .dtsx files to parse.

    - A single file  -> just that file.
    - A folder       -> top-level *.dtsx only (sorted).

    We deliberately do NOT recurse: SSIS packages live at the project root,
    while sub-folders like ``obj\\`` / ``bin\\`` hold build-output *copies* that
    would create duplicates and filename collisions.
    """
    if path.is_dir():
        return sorted(path.glob("*.dtsx"))
    return [path]
