import lsst.afw.table as afwTable
import lsst.meas.algorithms.detection as detection
import astropy.table
import lsst.geom
from time import time
import astropy.units as u
import sys
from joblib import Parallel, delayed
import logging
import numpy as np
from .catalog import DetectionCatalog, MultiEpochDetectionCatalog
from .serialize import read
from .images import Image
# import lsst.afw.detection as afwDet
# import lsst.afw.image as afwImage
# import lsst.afw.display as afwDisplay
# import matplotlib.pyplot as plt

logging.basicConfig()
log = logging.getLogger(__name__)

def detect(image : Image, threshold=3, no_masks=False):
    logging.basicConfig()
    log = logging.getLogger(__name__)

    if isinstance(image, Image):
        exposure = image.exposure
    else:
        exposure = image
        
    config = detection.SourceDetectionConfig()
    if no_masks:
        config.excludeMaskPlanes = []
    else:
        config.excludeMaskPlanes = [
            "EDGE",
            "SAT",
            "SUSPECT",
            "BAD",
            "NO_DATA",
            "STREAK",
            "CROSSTALK", # ? seems to cause noise
            "NOT_DEBLENDED" # ? seems to cause noise
        ]
    config.thresholdValue = threshold
    task = detection.SourceDetectionTask(config=config)

    exposure_date = exposure.visitInfo.getDate()
    half = (exposure.visitInfo.getExposureTime() / 2 + 1/2) / (24*60*60)
    exposure_date_jd = astropy.time.Time(exposure_date.get(exposure_date.JD) + half, format='jd', scale='utc')
    wcs = exposure.wcs
    point_to_sphere = wcs.getTransform().applyForward

    t2 = time()
    table = afwTable.SourceTable.makeMinimalSchema()
    results = task.run(table, exposure)
    t3 = time()

    footprintSet = results.positive
    footprints = footprintSet.getFootprints()
    peaks = []
    for footprint in footprints:
        peaks.append(footprint.peaks.asAstropy())
    peaks = astropy.table.vstack(peaks)
    t4 = time()
    coords = [point_to_sphere(lsst.geom.Point2D(_x, _y)) for _x, _y in zip(peaks['i_x'], peaks['i_y'])]
    ra = [float(c.getRa()) for c in coords]
    dec = [float(c.getDec()) for c in coords]
    peaks['ra'] = ra * u.radian
    peaks['dec'] = dec * u.radian
    t5 = time()

    masked = np.zeros(exposure.mask.array.shape)
    masked_pixel_summary = {
        "total": np.product(exposure.mask.array.shape),
    }
    for m in config.excludeMaskPlanes:
        mask_plane_dict = exposure.mask.getMaskPlaneDict()
        b = mask_plane_dict[m]
        bit_mask = (exposure.mask.array >> b) & 1
        masked_pixel_summary[m] = bit_mask.sum()
        masked += bit_mask

    masked_pixel_summary['masked'] = (masked > 0).sum()
    t6 = time()

    log.info("run: %s", t3 - t2)
    log.info("astropy: %s", t4 - t3)
    log.info("ra/dec: %s", t5 - t4)
    log.info("masks: %s", t6 - t5)
    log.info("found %s detections", len(peaks))

    return DetectionCatalog(peaks, exposure_date_jd, exposure.visitInfo.getId(), exposure.detector.getId(), masked_pixel_summary)

def main():
    import argparse
    import lsst.daf.butler as dafButler
    parser = argparse.ArgumentParser(prog=__name__)
    parser.add_argument("input", nargs="?", type=argparse.FileType('rb'), default=sys.stdin)
    parser.add_argument('output', nargs='?', type=argparse.FileType('wb'), default=sys.stdout)
    parser.add_argument("--threshold", type=float, required=True)
    parser.add_argument("--processes", type=int, default=1)
    parser.add_argument("--no-masks", action="store_true")

    args = parser.parse_args()

    images = read(args.input)
    
    log.info(f"detecting on {len(images)} images using {args.processes} processes to threshold {args.threshold}")
    t1 = time()
    single_epoch_catalogs = Parallel(n_jobs=args.processes)(delayed(detect)(image, threshold=args.threshold, no_masks=args.no_masks) for image in images)

    t2 = time()
    log.info(f"detection took {t2-t1} seconds")
    multi_epoch_catalog = MultiEpochDetectionCatalog(single_epoch_catalogs)
    multi_epoch_catalog.write(args.output)

if __name__ == "__main__":
    main()