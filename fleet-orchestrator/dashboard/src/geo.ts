import type { LatLngBoundsExpression, LatLngTuple } from "leaflet";
import type { Node } from "./types";

// Where grid (0, 0) sits on Earth. The scenario is in km, so 1 grid unit = 1 km.
// Farmland in the Imperial Valley, California, between El Centro and Holtville: no
// town inside the view, and the Alamo River runs under the "bridge" and
// "south crossing" roads. Check the satellite view if you move it.
export const LAT0 = 32.83;
export const LON0 = -115.497;

const KM_PER_DEG_LAT = 110.574;
const KM_PER_DEG_LON = 111.32 * Math.cos((LAT0 * Math.PI) / 180);

/** Grid (x east, y north, km) to [lat, lon]. */
export function toLatLng(x: number, y: number): LatLngTuple {
  return [LAT0 + y / KM_PER_DEG_LAT, LON0 + x / KM_PER_DEG_LON];
}

/** Map bounds around all nodes, with `padKm` of margin. */
export function boundsOf(nodes: Node[], padKm = 0.9): LatLngBoundsExpression {
  const xs = nodes.map((n) => n.x), ys = nodes.map((n) => n.y);
  return [
    toLatLng(Math.min(...xs) - padKm, Math.min(...ys) - padKm),
    toLatLng(Math.max(...xs) + padKm, Math.max(...ys) + padKm),
  ];
}
