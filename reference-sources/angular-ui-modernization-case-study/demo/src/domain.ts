export type Status = "New" | "Review" | "Ready";
export interface CaseRow {
  readonly id: string;
  readonly title: string;
  readonly team: string;
  readonly status: Status;
  readonly days: number;
}
export type SortKey = "id" | "title" | "days";
export interface Query {
  text: string;
  status: Status | "all";
  sort: SortKey;
  direction: "asc" | "desc";
}
export interface Draft {
  version: 1;
  owner: string;
  priority: "Normal" | "High";
  note: string;
}

// Original, synthetic fixtures. No customer or production data.
export const CASES: readonly CaseRow[] = [
  {
    id: "CASE-101",
    title: "Address correction",
    team: "Registry",
    status: "New",
    days: 2,
  },
  {
    id: "CASE-102",
    title: "Boundary review",
    team: "Geospatial",
    status: "Ready",
    days: 8,
  },
  {
    id: "CASE-103",
    title: "Map attachment check",
    team: "Geospatial",
    status: "Review",
    days: 12,
  },
  {
    id: "CASE-104",
    title: "Document completeness",
    team: "Registry",
    status: "Review",
    days: 5,
  },
  {
    id: "CASE-105",
    title: "Map export request",
    team: "Geospatial",
    status: "New",
    days: 1,
  },
  {
    id: "CASE-106",
    title: "Contact update",
    team: "Support",
    status: "Ready",
    days: 3,
  },
];

export function selectRows(rows: readonly CaseRow[], query: Query): CaseRow[] {
  const text = query.text.trim().toLowerCase();
  return rows
    .filter(
      (row) =>
        (query.status === "all" || row.status === query.status) &&
        `${row.id} ${row.title} ${row.team}`.toLowerCase().includes(text),
    )
    .sort((a, b) => {
      const order =
        query.sort === "days"
          ? a.days - b.days
          : a[query.sort].localeCompare(b[query.sort]);
      return (
        (query.direction === "asc" ? order : -order) || a.id.localeCompare(b.id)
      );
    });
}

export function toggleSelection(
  selected: ReadonlySet<string>,
  id: string,
): Set<string> {
  const next = new Set(selected);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

export function parseDraft(raw: string): Draft {
  const value: unknown = JSON.parse(raw);
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("Invalid draft");
  const d = value as Record<string, unknown>;
  if (
    d["version"] !== 1 ||
    typeof d["note"] !== "string" ||
    d["note"].length > 500 ||
    typeof d["owner"] !== "string" ||
    d["owner"].length > 80 ||
    (d["priority"] !== "Normal" && d["priority"] !== "High")
  )
    throw new Error("Invalid draft");
  return {
    version: 1,
    note: d["note"],
    owner: d["owner"],
    priority: d["priority"] as Draft["priority"],
  };
}
