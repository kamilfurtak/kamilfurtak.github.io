import { Component, Directive, input, output } from "@angular/core";
import { CaseRow, Query, SortKey } from "./domain";

// UI-neutral inputs/outputs. No PrimeNG events or Table instances cross this boundary.
@Directive()
export abstract class GridContract {
  readonly rows = input.required<CaseRow[]>();
  readonly selected = input.required<ReadonlySet<string>>();
  readonly query = input.required<Query>();
  readonly toggle = output<string>();
  readonly focusCase = output<string>();
  readonly sort = output<SortKey>();
  ariaSort(key: SortKey): "ascending" | "descending" | "none" {
    return this.query().sort === key
      ? this.query().direction === "asc"
        ? "ascending"
        : "descending"
      : "none";
  }
}

@Component({
  selector: "app-native-grid",
  template: `<div class="table-scroll">
    <table aria-label="Case queue" class="case-table">
      <thead>
        <tr>
          <th scope="col">Select</th>
          <th scope="col" [attr.aria-sort]="ariaSort('id')">
            <button (click)="sort.emit('id')">Case ID</button>
          </th>
          <th scope="col" [attr.aria-sort]="ariaSort('title')">
            <button (click)="sort.emit('title')">Subject</button>
          </th>
          <th scope="col">Status</th>
          <th scope="col" [attr.aria-sort]="ariaSort('days')">
            <button (click)="sort.emit('days')">Age in days</button>
          </th>
        </tr>
      </thead>
      <tbody>
        @for (row of rows(); track row.id) {
          <tr [class.selected]="selected().has(row.id)">
            <td>
              <input
                type="checkbox"
                [attr.aria-label]="'Select ' + row.id"
                [checked]="selected().has(row.id)"
                (change)="toggle.emit(row.id)"
              />
            </td>
            <td>
              <button class="case-link" (click)="focusCase.emit(row.id)">
                {{ row.id }}
              </button>
            </td>
            <td>
              {{ row.title }}<small>{{ row.team }}</small>
            </td>
            <td>
              <span class="status" [attr.data-status]="row.status">{{
                row.status
              }}</span>
            </td>
            <td>{{ row.days }}</td>
          </tr>
        } @empty {
          <tr>
            <td colspan="5">
              No cases match these filters. Clear the search or choose another
              status.
            </td>
          </tr>
        }
      </tbody>
    </table>
  </div>`,
})
export class NativeGridComponent extends GridContract {}
