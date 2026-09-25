import { describe, expect, it } from "vitest";
import {
  selectRows,
  toggleSelection,
  parseDraft,
  CASES,
  type Query,
} from "./domain";

const query: Query = { text: "", status: "all", sort: "id", direction: "asc" };
describe("UI-neutral table contract", () => {
  it("combines case-insensitive text and status filters", () => {
    expect(
      selectRows(CASES, { ...query, text: " MAP ", status: "Review" }).map(
        (r) => r.id,
      ),
    ).toEqual(["CASE-103"]);
  });
  it("sorts numerically without changing the source", () => {
    const before = [...CASES];
    const rows = selectRows(CASES, {
      ...query,
      sort: "days",
      direction: "desc",
    });
    expect(rows[0].days).toBe(12);
    expect(rows.at(-1)?.days).toBe(1);
    expect(CASES).toEqual(before);
  });
  it("returns an explicit empty result", () => {
    expect(selectRows(CASES, { ...query, text: "not a case" })).toEqual([]);
  });
  it("does not mutate selected IDs and allows deselecting", () => {
    const original = new Set(["CASE-101"]);
    const next = toggleSelection(original, "CASE-103");
    expect([...next]).toEqual(["CASE-101", "CASE-103"]);
    expect([...original]).toEqual(["CASE-101"]);
    expect([...toggleSelection(next, "CASE-101")]).toEqual(["CASE-103"]);
  });
});

describe("Untrusted local draft boundary", () => {
  it("accepts only the versioned current shape", () => {
    const draft = {
      version: 1,
      note: "Review the geometry",
      owner: "Alex",
      priority: "Normal",
    };
    expect(parseDraft(JSON.stringify(draft))).toEqual(draft);
  });
  it.each([
    "{",
    "null",
    "[]",
    '{"version":2}',
    '{"version":1,"note":true,"owner":"Alex","priority":"Normal"}',
  ])("rejects invalid persisted data: %s", (raw) => {
    expect(() => parseDraft(raw)).toThrow();
  });
  it("rejects unbounded notes and unknown priority values", () => {
    expect(() =>
      parseDraft(
        JSON.stringify({
          version: 1,
          note: "x".repeat(501),
          owner: "Alex",
          priority: "Normal",
        }),
      ),
    ).toThrow();
    expect(() =>
      parseDraft(
        JSON.stringify({ version: 1, note: "", owner: "", priority: "Admin" }),
      ),
    ).toThrow();
  });
});
