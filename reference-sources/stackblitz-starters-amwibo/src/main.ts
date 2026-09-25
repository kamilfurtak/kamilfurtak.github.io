import { Component } from '@angular/core';
import { bootstrapApplication } from '@angular/platform-browser';
import 'zone.js';
import { AngularOpenlayersModule } from 'ng-openlayers';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [AngularOpenlayersModule],
  template: ` 
<div style="height: 500px">
    <aol-map>
    <aol-view [zoom]="2">
      <aol-coordinate [x]="5.795122" [y]="45.210225" [srid]="'EPSG:4326'"></aol-coordinate>
    </aol-view>
    <aol-layer-tile>
      <aol-source-osm></aol-source-osm>
    </aol-layer-tile>
    <aol-interaction-default></aol-interaction-default>
    <aol-control-scaleline></aol-control-scaleline>
    <aol-control-zoomslider></aol-control-zoomslider>
    <aol-control-zoom></aol-control-zoom>
  </aol-map>
</div>

  `,
})
export class App {
  name = 'Angular';
}

bootstrapApplication(App);
