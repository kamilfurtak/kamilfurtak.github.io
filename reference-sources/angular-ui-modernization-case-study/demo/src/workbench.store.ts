import {
  computed,
  DestroyRef,
  inject,
  Injectable,
  InjectionToken,
  signal,
} from "@angular/core";
import {
  CASES,
  CaseRow,
  Query,
  selectRows,
  SortKey,
  toggleSelection,
} from "./domain";

export interface CaseApi {
  load(fail: boolean): Promise<readonly CaseRow[]>;
}
export const CASE_API = new InjectionToken<CaseApi>("CASE_API", {
  factory: () => ({
    async load(fail) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      if (fail) throw new Error("Simulated service outage");
      return CASES;
    },
  }),
});

@Injectable()
export class WorkbenchStore {
  private readonly api = inject(CASE_API);
  private generation = 0;
  readonly rows = signal<readonly CaseRow[]>([]);
  readonly query = signal<Query>({
    text: "",
    status: "all",
    sort: "id",
    direction: "asc",
  });
  readonly selected = signal<ReadonlySet<string>>(new Set());
  readonly focusedId = signal<string | null>(null);
  readonly loading = signal(false);
  readonly error = signal("");
  readonly visible = computed(() => selectRows(this.rows(), this.query()));
  readonly focused = computed(() =>
    this.rows().find((row) => row.id === this.focusedId()),
  );
  readonly hiddenSelected = computed(
    () =>
      [...this.selected()].filter(
        (id) => !this.visible().some((row) => row.id === id),
      ).length,
  );

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      this.generation++;
    });
  }
  filter(update: Partial<Query>): void {
    this.query.update((query) => ({ ...query, ...update }));
  }
  sortBy(sort: SortKey): void {
    this.query.update((query) => ({
      ...query,
      sort,
      direction:
        query.sort === sort && query.direction === "asc" ? "desc" : "asc",
    }));
  }
  toggle(id: string): void {
    this.selected.update((ids) => toggleSelection(ids, id));
  }
  async reload(fail = false): Promise<void> {
    const generation = ++this.generation;
    this.loading.set(true);
    this.error.set("");
    try {
      const rows = await this.api.load(fail);
      if (generation !== this.generation) return;
      this.rows.set(rows);
      this.selected.update(
        (ids) =>
          new Set([...ids].filter((id) => rows.some((row) => row.id === id))),
      );
      if (!rows.some((row) => row.id === this.focusedId()))
        this.focusedId.set(null);
    } catch {
      if (generation === this.generation)
        this.error.set(
          "Simulated request failed. Existing rows and your selection are preserved. Retry to recover.",
        );
    } finally {
      if (generation === this.generation) this.loading.set(false);
    }
  }
}
