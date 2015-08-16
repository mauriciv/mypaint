# This file is part of MyPaint.
# Copyright (C) 2009-2011 by Martin Renold <martinxyz@gmx.ch>
# Copyright (C) 2011-2015 by the MyPaint Development Team.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

## Imports

import time
import struct
import zlib
import numpy
import math
from logging import getLogger
logger = getLogger(__name__)

import mypaintlib

import tiledsurface
import idletask

TILE_SIZE = N = mypaintlib.TILE_SIZE


## Class defs

class StrokeShape (object):
    """The shape of a single brushstroke.

    This class stores the shape of a stroke in as a 1-bit bitmap. The
    information is stored in compressed memory blocks of the size of a
    tile (for fast lookup).

    """
    def __init__(self):
        """Construct a new, blank StrokeShape."""
        object.__init__(self)
        self.tasks = idletask.Processor()
        self.strokemap = {}
        self.brush_string = None

    @classmethod
    def new_from_snapshots(cls, before, after):
        """Build a new StrokeShape from before+after pair of snapshots.

        :param before: snapshot of the layer before the stroke
        :type before: lib.tiledsurface._TiledSurfaceSnapshot
        :param after: snapshot of the layer after the stroke
        :type after: lib.tiledsurface._TiledSurfaceSnapshot
        :returns: A new StrokeShape, or None.

        If the snapshots haven't changed, None is returned. In this
        case, no StrokeShape should be recorded.

        """
        before_dict = before.tiledict
        after_dict = after.tiledict
        before_tiles = set(before_dict.iteritems())
        after_tiles = set(after_dict.iteritems())
        changed_idxs = set(
            pos for pos, data
            in before_tiles.symmetric_difference(after_tiles)
        )
        if not changed_idxs:
            return None
        shape = cls()
        assert not shape.strokemap
        shape.tasks.add_work(_TileDiffUpdateTask(
            before.tiledict,
            after.tiledict,
            changed_idxs,
            shape.strokemap,
        ))
        return shape

    def init_from_string(self, data, translate_x, translate_y):
        assert not self.strokemap
        assert translate_x % N == 0
        assert translate_y % N == 0
        translate_x /= N
        translate_y /= N
        while data:
            tx, ty, size = struct.unpack('>iiI', data[:3*4])
            compressed_bitmap = data[3*4:size+3*4]
            self.strokemap[tx + translate_x, ty + translate_y] = compressed_bitmap
            data = data[size+3*4:]

    def save_to_string(self, translate_x, translate_y):
        assert translate_x % N == 0
        assert translate_y % N == 0
        translate_x /= N
        translate_y /= N
        self.tasks.finish_all()
        data = ''
        for (tx, ty), compressed_bitmap in self.strokemap.iteritems():
            tx, ty = tx + translate_x, ty + translate_y
            data += struct.pack('>iiI', tx, ty, len(compressed_bitmap))
            data += compressed_bitmap
        return data

    def _complete_tile_tasks(self, pred):
        """Complete all queued work on a subset of tiles.

        :param callable pred: Tile index predicate, f((tx,ty)) -> bool

        This will cause only a predicate-limited subset of the work in
        the task queue to be forced to completion, if possible. If not,
        the entire task queue is completed.

        """
        tileproc_methods = []
        for task in self.tasks.iter_work():
            try:
                tileproc = task[0].process_tile_subset
            except AttributeError:
                tileproc_methods = []
                break
            else:
                tileproc_methods.append(tileproc)
        if tileproc_methods:
            for tileproc in tileproc_methods:
                tileproc(pred)
        else:
            self.tasks.finish_all()

    def touches_pixel(self, x, y):
        """Returns whether the stroke shape hits a specific pixel

        :param int x: Pixel X position.
        :param int y: Pixel Y position.
        :returns: True if (x, y) is a set pixel in this shape's bitmap.
        :rtype: bool

        """
        x = int(x)
        y = int(y)
        pixel_ti = (x/N, y/N)
        pred = lambda ti: (ti == pixel_ti)
        self._complete_tile_tasks(pred)
        data = self.strokemap.get(pixel_ti)
        if data:
            data = numpy.fromstring(zlib.decompress(data), dtype='uint8')
            data.shape = (N, N)
            return bool(data[y % N, x % N])
        return False

    def render_to_surface(self, surf, bbox=None, center=None):
        """Draw all or part of the shape to a tile-accessible surface.

        :param lib.surface.TileAccessible surf: target surface
        :param tuple bbox: pixel bounding box (x,y,w,h) to render

        If the bbox parameter is specified, only tiles within the
        bounding box will be rendered.

        """
        pred = _TileIndexPredicate(bbox=bbox, center=center, radius=1000)
        self._complete_tile_tasks(pred)
        tile_idxs = self.strokemap.keys()
        for tx, ty in tile_idxs:
            if not pred((tx, ty)):
                continue
            data = self.strokemap[(tx, ty)]
            data = numpy.fromstring(zlib.decompress(data), dtype='uint8')
            data.shape = (N, N)
            with surf.tile_request(tx, ty, readonly=False) as tile:
                # neutral gray, 50% opaque
                tile[:, :, 3] = data.astype('uint16') * (1 << 15)/2
                tile[:, :, 0] = tile[:, :, 3]/2
                tile[:, :, 1] = tile[:, :, 3]/2
                tile[:, :, 2] = tile[:, :, 3]/2

    def translate(self, dx, dy):
        """Translate the shape by (dx, dy)"""
        self.tasks.finish_all()
        tmp = {}
        self.tasks.add_work(_TileTranslateTask(self.strokemap, tmp, dx, dy))
        self.tasks.add_work(_TileRecompressTask(tmp, self.strokemap))

    def trim(self, rect):
        """Trim the shape to a rectangle, discarding data outside it

        :param rect: A trimming rectangle in model coordinates
        :type rect: tuple (x, y, w, h)
        :returns: Whether anything remains after the trim
        :rtype: bool

        Only complete tiles are discarded by this method.
        """
        self.tasks.finish_all()
        x, y, w, h = rect
        logger.debug("Trimming stroke to %dx%d%+d%+d", w, h, x, y)
        for tx, ty in list(self.strokemap.keys()):
            if tx*N+N < x or ty*N+N < y or tx*N > x+w or ty*N > y+h:
                self.strokemap.pop((tx, ty))
        return bool(self.strokemap)


class _TileDiffUpdateTask:
    """Idle task: update strokemap with tile & pixel diffs of snapshots.

    This task is used during initialization of the StrokeShape.

    """

    def __init__(self, before, after, changed_idxs, targ):
        """Initialize, ready to update a target StrokeShape with diffs

        :param dict before: Complete pre-stroke tiledict (RO, {xy:Tile})
        :param dict after: Complete post-stroke tiledict (RO, {xy:Tile})
        :param set changed_idxs: RW set of (x,y) tile indexes to process
        :param dict targ: Target strokemap (WO, {xy: bytes})

        """
        self._before_dict = before
        self._after_dict = after
        self._targ_dict = targ
        self._remaining = changed_idxs

    def __repr__(self):
        return "<{name} remaining={remaining}>".format(
            name = self.__class__.__name__,
            remaining = len(self._remaining),
        )

    def __call__(self):
        """Diff and update one queued tile."""
        try:
            ti = self._remaining.pop()
        except KeyError:
            return False
        self._update_tile(ti)
        return bool(self._remaining)

    def process_tile_subset(self, pred):
        """Diff and update a subset of queued tiles now."""
        processed = set()
        for ti in self._remaining:
            if not pred(ti):
                continue
            self._update_tile(ti)
            processed.add(ti)
        self._remaining -= processed

    def _update_tile(self, ti):
        """Diff and update the tile at a specified position."""
        transparent = tiledsurface.transparent_tile
        data_before = self._before_dict.get(ti, transparent).rgba
        data_after = self._after_dict.get(ti, transparent).rgba
        differences = numpy.empty((N, N), 'uint8')
        mypaintlib.tile_perceptual_change_strokemap(
            data_before,
            data_after,
            differences,
        )
        self._targ_dict[ti] = zlib.compress(differences.tostring())


class _TileTranslateTask:
    """Translate/move tiles (compressed strokemap -> uncompressed tmp)

    Calling this task is destructive to the source strokemap, so it must
    be paired with a _TileRecompressTask queued up to fire when it has
    completely finished.

    Tiles are translated by slicing and recombining, so this task must
    be called to completion before the output tiledict will be ready for
    recompression.

    """

    def __init__(self, src, targ, dx, dy):
        """Initialize with source and target.

        :param dict src: compressed strokemap, RW {xy: bytes}
        :param dict targ: uncompressed tiledict, RW {xy: array}
        :param int dx: x offset for the translation, in pixels
        :param int dy: y offset for the translation, in pixels

        """
        self._src = src
        self._targ = targ
        self._dx = int(dx)
        self._dy = int(dy)
        self._slices_x = tiledsurface.calc_translation_slices(self._dx)
        self._slices_y = tiledsurface.calc_translation_slices(self._dy)

    def __repr__(self):
        return "<{name} dx={dx} dy={dy}>".format(
            name = self.__class__.__name__,
            dx = self._dx,
            dy = self._dy,
        )

    def __call__(self):
        """Idle task: translate a single tile into the output dict.

        """
        try:
            (src_tx, src_ty), src = self._src.popitem()
        except KeyError:
            return False
        src = numpy.fromstring(zlib.decompress(src), dtype='uint8')
        src.shape = (N, N)
        slices_x = self._slices_x
        slices_y = self._slices_y
        is_integral = len(slices_x) == 1 and len(slices_y) == 1
        for (src_x0, src_x1), (targ_tdx, targ_x0, targ_x1) in slices_x:
            for (src_y0, src_y1), (targ_tdy, targ_y0, targ_y1) in slices_y:
                targ_tx = src_tx + targ_tdx
                targ_ty = src_ty + targ_tdy
                if is_integral:
                    self._targ[targ_tx, targ_ty] = src
                else:
                    targ = self._targ.get((targ_tx, targ_ty), None)
                    if targ is None:
                        targ = numpy.zeros((N, N), 'uint8')
                        self._targ[targ_tx, targ_ty] = targ
                    targ[targ_y0:targ_y1, targ_x0:targ_x1] \
                        = src[src_y0:src_y1, src_x0:src_x1]
        return bool(self._src)


class _TileRecompressTask:
    """Re-compress data after a move (uncomp. tmp -> comp. strokemap)"""

    def __init__(self, src, targ):
        """Initialize with source and target.

        :param dict src: input uncompressed tiledict (WO, {xy:array})
        :param dict targ: output compressed strokemap (WO, {xy:bytes})

        """
        self._src_dict = src
        self._targ_dict = targ

    def __call__(self):
        """Compress & store an arbitrary queued tile's data."""
        try:
            ti, array = self._src_dict.popitem()
        except KeyError:
            return False
        self._compress_tile(ti, array)
        return len(self._src_dict) > 0

    def process_tile_subset(self, pred):
        """Compress & store a subset of queued tiles' data now."""
        processed = []
        for ti in self._src_dict.iterkeys():
            if not pred(ti):
                continue
            self._compress_tile(ti, self._src_dict[ti])
            processed.append(ti)
        for ti in processed:
            self._src_dict.pop(ti)

    def _compress_tile(self, ti, array):
        if not array.any():
            return
        self._targ_dict[ti] = zlib.compress(array.tostring())

    def __repr__(self):
        return "<{name} remaining={n}>".format(
            name = self.__class__.__name__,
            n = len(self._src_dict),
        )


## Helper funcs


class _TileIndexPredicate (object):
    """Tile index tester callable for processing subsets of tiles."""

    def __init__(self, bbox=None, center=None, radius=None):
        """Initialize with selection criteria

        :param tuple bbox: A limiting bbox, as (x, y, w, h), in pixels.
        :param tuple center: Center of interest, as (x, y), in pixels.
        :param int radius: Interest radius, in pixels.

        Center and radius should define where the user is looking and
        expects to see the immediate result. The bounding box should
        reflect the UI viewport for large portions of interest, but can
        be just a single pixel. The center should be within this bbox.

        """
        self._tile_range = None
        if bbox:
            self._tile_range = _pixel_bbox_to_tile_range(bbox)
        self._center_tile = None
        self._max_tile_dist = None
        if center and radius:
            self._center_tile = (center[0]/N, center[1]/N)
            self._max_tile_dist = max(1, radius/N)

    def __call__(self, ti):
        """Test a tile index, return True if it should be selected.
        """
        tx, ty = ti
        if self._tile_range:
            if not _tile_in_range(ti, self._tile_range):
                return False
        if self._center_tile and self._max_tile_dist:
            ctx, cty = self._center_tile
            td = math.hypot(ctx-tx, cty-ty)
            return (td <= self._max_tile_dist)
        return True


def _pixel_bbox_to_tile_range(bbox):
    """Convert a pixel area to testable ranges of tiles.

    :param tuple bbox: The area to complete, as pixel (x, y, w, h)
    :returns: Tile ranges, as (txmin, txmax, tymin, tymax).
    :rtype: tuple

    The returned ranges allow tile indices to be tested as, e.g.,

    >>> bbox = (63, 64, 1, 1)
    >>> txa, txb, tya, tyb = _pixel_bbox_to_tile_range(bbox)
    >>> (txa, txb)
    (0, 1)
    >>> (tya, tyb)
    (1, 2)
    >>> txa <= 0 < txb
    True
    >>> tya <= 0 < tyb
    False

    As the name suggests, the returned ranges can be used with the
    builtin range() function.

    See also `_tile_in_ranges()`.

    """
    x, y, w, h = bbox
    n = float(N)
    txmin = int(math.floor(x / n))
    txmax = int(math.ceil((x + w) / n))
    tymin = int(math.floor(y / n))
    tymax = int(math.ceil((y + h) / n))
    return (txmin, txmax, tymin, tymax)


def _tile_in_range(ti, trange):
    """Tests whether a tile index is within a range.

    :param tuple ti: tile index, as (tx, ty).
    :param tuple trange: ranges, as (txmin, txmax, tymin, tymax).
    :rtype: bool

    This function expects the kinds of ranges returned by
    _pixel_bbox_to_tile_range().

    >>> bbox = (63, 64, 1, 1)
    >>> range = _pixel_bbox_to_tile_range(bbox)
    >>> _tile_in_range((1, 1), range)
    False
    >>> _tile_in_range((0, 1), range)
    True

    """
    tx, ty = ti
    txa, txb, tya, tyb = trange
    return (txa <= tx < txb) and (tya <= ty < tyb)
