import logging
import sys
from functools import lru_cache
import numpy as np
import astropy.units as u
from astropy.coordinates import SkyCoord
from ..serialize import read, write

logging.basicConfig()
log = logging.getLogger(__name__)

@lru_cache(maxsize=512)
def match_time_to_exposure(t, catalog):
    e = list(set(catalog.exposure[np.abs(catalog.time - t) < 1/(24*60*60)]))
    return e

def _recover(fakes, clusters, catalog, match_threshold=1 * u.arcsec):
    """
    What I want to know is for each fake:
    - how many clusters did it appear in
    - which clusters did it appear in
    - how often did it appear in those clusters?
    The problem is that the match is on ra/dec/time but the connection is only implicit
    unless the catalog is provided

    I could do:
    - for each exposure
        make exposure_catalog
        for each cluster
            get points from exposure_catalog
    """
    recoveries = {}
    for orbit in fakes.group_by("ORBITID").groups:
        orbit_id = orbit[0]['ORBITID']
        fake_coords = SkyCoord(orbit['RA'] * u.deg, orbit['DEC'] * u.deg)

        fakes_lookup = {}
        recovery = {}
        for i, cluster in enumerate(clusters):
            for x, y, t in cluster.points:
                exposure = match_time_to_exposure(t, catalog)[0]
                if exposure not in fakes_lookup:
                    fakes_lookup[exposure] = fake_coords[orbit['EXPNUM'] == exposure]

            cluster_coords = SkyCoord(cluster.points[:, 0] * u.deg, cluster.points[:, 1] * u.deg)
            fakes_in_cluster = 0
            for j, (coord, t) in enumerate(zip(cluster_coords, cluster.points[:, 2])):
                exposure = match_time_to_exposure(t, catalog)[0]
                fake_coord = fakes_lookup[exposure]
                if len(fake_coord) == 0:
                    continue

                if len(fake_coord) > 1:
                    print(orbit_id, exposure, len(fake_coord))
                    
                fake_coord = fake_coord[0]
                sep = fake_coord.separation(coord)
                if sep < match_threshold:
                    fakes_in_cluster += 1

            if fakes_in_cluster != 0:
                recovery[i] = fakes_in_cluster
        recoveries[orbit_id] = recovery

    return recoveries

def recover(fakes, clusters, catalog, match_threshold_points=1/3600, match_threshold_line=1/3600, projection=None, hough=None):
    import astropy.table
    from salad.regression import RegressionResult

    matches = {orbit : {"points": {}, "line": {}, "catalog": 0, "info": {}} for orbit in set(fakes['ORBITID'])}

    c_fake = SkyCoord(fakes['RA']*u.deg, fakes['DEC']*u.deg)
    t_fake = fakes['mjd_mid'] * u.day
        
    t1 = astropy.table.Table(data=[c_fake.ra, c_fake.dec, fakes['ORBITID'], fakes['EXPNUM']], names=['ra', 'dec', 'orbit', 'expnum'])

    t_points = []
    t_line = []
    for i, k in enumerate(clusters):
        cluster = clusters[k]
        # recover points
        e = cluster.points[:, -1].astype(int)
        c_cluster = SkyCoord(cluster.points[:, 0] * u.deg, cluster.points[:, 1] * u.deg)
        t2 = astropy.table.Table(
            data=[[k]*len(e), c_cluster.ra, c_cluster.dec, e], 
            names=['cluster', 'ra', 'dec', 'expnum']
        )
        t_points.append(t2)
        
        # recover line
        # if 'line' in cluster.extra:
        #     line = cluster.extra['line']
        # elif 'b' in cluster.extra:
        #     alpha = np.array(
        #         [
        #             (cluster.extra['x'] * hough.dx + hough.min_x), 
        #             (cluster.extra['y'] * hough.dy + hough.min_y)
        #         ]
        #     ) * u.deg
        #     beta = projection.directions.b[cluster.extra['b']][None, :]
        #     line = RegressionResult()
        #     line.beta = beta
        #     line.alpha = alpha
        #     t_fake = (fakes['mjd_mid'].data[:, None] - projection.reference_time)
        
        line_locations = cluster.line.predict(t_fake)#t_fake @ cluster.line.beta * u.day + line.alpha
        e = fakes['EXPNUM']
        o = fakes['ORBITID']
        c_line = SkyCoord(line_locations[:, 0], line_locations[:, 1])
        t2 = astropy.table.Table(
            data=[[k]*len(e), c_line.ra, c_line.dec, e, o], 
            names=['cluster', 'ra', 'dec', 'expnum', 'orbit']
        )
        t_line.append(t2)        


    if len(t_points) > 0:
        t_points = astropy.table.vstack(t_points)
        # this matches cluster points to fake detections    
        for g in astropy.table.join(t1, t_points, keys=['expnum']).group_by(["orbit", "cluster"]).groups:
            sep = SkyCoord(g['ra_1'], g['dec_1']).separation(SkyCoord(g['ra_2'], g['dec_2']))
            n_matches = (sep.value < match_threshold_points).sum()
            o = int(g[0]['orbit'])
            c = int(g[0]['cluster'])
            if n_matches > 0:
                log.info("orbit %d matches to %d points from cluster %d", o, n_matches, c)
                matches[o]['points'][c] = {
                    "fake": n_matches, # number of points that match to a fake
                    "total": len(clusters[c].points), # number of points in this cluster
                }

    if len(t_line) > 0:
        t_line = astropy.table.vstack(t_line)
        # this matches cluster lines to fake detections
    #     matches_line = {orbit : {} for orbit in set(fakes['ORBITID'])}
        for g in astropy.table.join(t1, t_line, keys=['expnum', 'orbit']).group_by(["orbit", "cluster"]).groups:
            sep = SkyCoord(g['ra_1'], g['dec_1']).separation(SkyCoord(g['ra_2'], g['dec_2']))
            n_matches = (sep.value < match_threshold_line).sum()
            o = int(g[0]['orbit'])
            c = int(g[0]['cluster'])
            if n_matches > 0:
                log.info("orbit %d matches to line %d times from cluster %d", o, n_matches, c)
                matches[o]['line'][c] = {
                    "fake": n_matches, # number of matches to fakes
                    "total": len(fakes[fakes['ORBITID'] == o]), # number of injected fakes
                }
    
    # this matches catalog points to fake detections
    x = catalog.X(columns=['ra', 'dec', 'time', 'exposures'])
    c_catalog = SkyCoord(x[:, 0]*u.deg, x[:, 1]*u.deg)
    t_catalog = astropy.table.Table(
        data=[c_catalog.ra, c_catalog.dec, x[:, 3]], 
        names=['ra', 'dec', 'expnum']
    )
    for g in astropy.table.join(t1, t_catalog, keys=['expnum']).group_by(["orbit"]).groups:
        sep = SkyCoord(g['ra_1'], g['dec_1']).separation(SkyCoord(g['ra_2'], g['dec_2']))
        n_matches = (sep.value < match_threshold_points).sum()
        o = int(g[0]['orbit'])
        matches[o]['catalog'] = {
            "fake": n_matches, # number of matches to fakes
            "total": len(fakes[fakes['ORBITID'] == o]), # number of injected fakes
        }

    for g in fakes.group_by("ORBITID").groups:
        idx = np.argsort(g['mjd_mid'])
        dt = (g[idx]['mjd_mid'][-1] - g[idx]['mjd_mid'][0]) * u.day

        if dt == 0:
            vra = np.nan
            vdec = np.nan
        else:
            dra = (g[idx]['RA'][-1] - g[idx]['RA'][0]) * u.deg
            ddec = (g[idx]['DEC'][-1] - g[idx]['DEC'][0]) * u.deg
            vra = dra / dt
            vdec = ddec / dt

        o = int(g[0]['ORBITID'])
        matches[o]['info'] = dict(
            vra=vra,
            vdec=vdec,
        )

        if hough is not None and not np.isnan(vra):
            dv = hough.projection.directions.b - np.array([vra.value, vdec.value]) * u.deg/u.day
            dv_s = ((dv**2).sum(axis=1)**0.5)
            min_dv_idx = dv_s.argmin()
            min_dv = dv_s[min_dv_idx]
            distance = min_dv * dt
            matches[o]['info']['closest_dir'] = hough.projection.directions.b[min_dv_idx]
            matches[o]['info']['min_dv'] = min_dv
            matches[o]['info']['distance'] = distance
            matches[o]['info']['findable'] = (distance < hough.dx * u.deg)
        
    return matches

def main():
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog=__name__)
    parser.add_argument("--fakes", type=str, required=True)
    parser.add_argument("--clusters", type=str, required=True)
    parser.add_argument("--catalog", type=str, required=True)
    parser.add_argument("--hough", type=str, default=None)
    parser.add_argument("--projection", type=str, default=None)
    parser.add_argument('output', nargs='?', type=argparse.FileType('wb'), default=sys.stdout)
    parser.add_argument("--threshold-points", type=float, default=1)
    parser.add_argument("--threshold-line", type=float, default=1)
    parser.add_argument("--threshold-unit", type=str, default="arcsec")
    
    args = parser.parse_args()

    fakes = read(args.fakes)
    clusters = read(args.clusters)
    catalog = read(args.catalog)
    if args.hough:
        hough = read(args.hough)
    else:
        hough = args.hough
    if args.projection:
        projection = read(args.projection)
    else:
        projection = args.projection

    threshold_points = (args.threshold_points * getattr(u, args.threshold_unit)).to(u.deg).value
    threshold_line = (args.threshold_line * getattr(u, args.threshold_unit)).to(u.deg).value
    recoveries = recover(fakes, clusters, catalog, hough=hough, projection=projection, match_threshold_points=threshold_points, match_threshold_line=threshold_line)
    write(recoveries, args.output)

if __name__ == "__main__":
    main()