import type { LatLngBoundsExpression, LatLngTuple } from "leaflet";
import type { Node } from "./types";

// Every road in the scenario runs east-west or north-south, so each grid column
// (x) and row (y) the farm uses is pinned to a real farm road in the Imperial
// Valley, California, east of Imperial (positions from OpenStreetMap). The Alamo
// River runs under the "bridge" and "south crossing" roads, like in the scenario. Grid values in between are
// interpolated, so a tractor driving along a column or row stays on that real road.
// The real roads are about 1.6 km apart per grid unit, so the satellite view is
// drawn at roughly 1.6x the sim's km. Check the satellite view if you change these.
const X_ROADS: [number, number][] = [[1, -115.52647], [3, -115.48374], [6, -115.42342], [9, -115.38048]]; // [grid x, lon]
const Y_ROADS: [number, number][] = [[2, 32.84734], [5, 32.88355], [8, 32.92378]]; // [grid y, lat]

const KM_PER_DEG_LAT = 110.574;
const KM_PER_DEG_LON = 111.32 * Math.cos((Y_ROADS[0][1] * Math.PI) / 180);

/** Piecewise-linear lookup in a sorted [grid, degrees] table; extends the end segments. */
function interp(table: [number, number][], v: number): number {
  let i = 0;
  while (i < table.length - 2 && v > table[i + 1][0]) i++;
  const [g0, d0] = table[i], [g1, d1] = table[i + 1];
  return d0 + ((v - g0) / (g1 - g0)) * (d1 - d0);
}

/** Grid (x east, y north) to [lat, lon] on the real roads. */
export function toLatLng(x: number, y: number): LatLngTuple {
  return [interp(Y_ROADS, y), interp(X_ROADS, x)];
}

/** A square `sideKm` wide on the ground, with its south-west corner `gapKm` north-east of grid point (x, y). */
export function squareBeside(x: number, y: number, sideKm: number, gapKm = 0.06): LatLngBoundsExpression {
  const [lat, lon] = toLatLng(x, y);
  const dLat = (km: number) => km / KM_PER_DEG_LAT, dLon = (km: number) => km / KM_PER_DEG_LON;
  return [
    [lat + dLat(gapKm), lon + dLon(gapKm)],
    [lat + dLat(gapKm + sideKm), lon + dLon(gapKm + sideKm)],
  ];
}

/** Map bounds around all nodes, with `pad` grid units of margin. */
export function boundsOf(nodes: Node[], pad = 0.6): LatLngBoundsExpression {
  const xs = nodes.map((n) => n.x), ys = nodes.map((n) => n.y);
  return [
    toLatLng(Math.min(...xs) - pad, Math.min(...ys) - pad),
    toLatLng(Math.max(...xs) + pad, Math.max(...ys) + pad),
  ];
}
