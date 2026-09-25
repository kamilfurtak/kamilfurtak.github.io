import { Component } from "@angular/core";
import { TableModule } from "primeng/table";
import { GridContract } from "./grid-adapters";

@Component({
  selector: "app-prime-grid",
  imports: [TableModule],
  template: `<div class="table-scroll prime-table">
    <p-table
      [value]="rows()"
      dataKey="id"
      [tableStyle]="{ 'min-width': '35rem' }"
    >
      <ng-template #header
        ><tr>
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
        </tr></ng-template
      >
      <ng-template #body let-row
        ><tr [class.selected]="selected().has(row.id)">
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
        </tr></ng-template
      >
      <ng-template #emptymessage
        ><tr>
          <td colspan="5">
            No cases match these filters. Clear the search or choose another
            status.
          </td>
        </tr></ng-template
      >
    </p-table>
  </div>`,
})
export class PrimeGridComponent extends GridContract {}
