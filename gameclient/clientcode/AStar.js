goog.provide('AStar');

goog.require('goog.array');
goog.require('BinaryHeap');

/*
  A* Pathfinding

  (Very heavily) modified from http://github.com/bgrins/javascript-astar
  Copyright (c) Brian Grinstead, http://briangrinstead.com

  Permission is hereby granted, free of charge, to any person obtaining
  a copy of this software and associated documentation files (the
  "Software"), to deal in the Software without restriction, including
  without limitation the rights to use, copy, modify, merge, publish,
  distribute, sublicense, and/or sell copies of the Software, and to
  permit persons to whom the Software is furnished to do so, subject to
  the following conditions:

  The above copyright notice and this permission notice shall be
  included in all copies or substantial portions of the Software.

  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
  EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
  MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
  NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
  LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
  OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
  WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
*/

// SP3RDPARTY : javascript-astar : MIT License

/** @const */
var AStar = {
    ASTAR_DEBUG: 0,
    ASTAR_MAX_ITER: 999999 // limit iterations to prevent infinite loop
};

// DISTANCE HEURISTICS
// See list of heuristics: http://theory.stanford.edu/~amitp/GameProgramming/Heuristics.html

/** @param {Array.<number>} start_pos
  * @param {Array.<number>} cur_pos
  * @param {Array.<number>} end_pos */
AStar.heuristic_manhattan = function(start_pos, cur_pos, end_pos) {
    // Manhattan distance
    var dx1 = Math.abs(end_pos[0] - cur_pos[0]);
    var dy1 = Math.abs(end_pos[1] - cur_pos[1]);
    return dx1 + dy1;
};

/** @param {Array.<number>} start_pos
  * @param {Array.<number>} cur_pos
  * @param {Array.<number>} end_pos */
AStar.heuristic_euclidean = function(start_pos, cur_pos, end_pos) {
    // Euclidean distance with cross-product tie-breaker
    // tends to make "cleaner" paths and visit fewer cells, but tests a little slower than manhattan
    var dx1 = cur_pos[0] - end_pos[0], dy1 = cur_pos[1] - end_pos[1];
    var dx2 = start_pos[0] - end_pos[0], dy2 = start_pos[1] - end_pos[1];
    var cross = Math.abs(dx1*dy2 - dx2*dy1);
    return Math.abs(dx1)+Math.abs(dy1) + 0.001*cross;
};

// MAP CELL

// by default, we track whether a cell is blocked by a single "block_count" counter.
// for more complex situations, you can use the blockers list to associate state with
// blockage, and then use a "checker" function to test against that state.

/** Function to evaluate the blockage of a cell. Return true if blocked, false otherwise.
 * @typedef {function(AStar.AStarCell): boolean | null} */
AStar.BlockChecker;

/** Function to evaluate the blockage of a cell when reached via a specific path.
 *  Note that path dependence may invalidate the A* algorithm.
 *  The second parameter is the array of coordinates along the path, excluding starting point, including ending point.
 * @typedef {function(AStar.AStarCell, Array.<Array.<number>>): boolean | null} */
AStar.PathChecker;

/** @constructor
 * @param {Array.<number>} pos
 */
AStar.AStarCell = function(pos) {
    this.pos = pos;
    this.block_count = 0; // count of obstacles overlapping this point

    /** @type {Array|null} list of references to obstacles that are blocking here */
    this.blockers = null; // optional - only used by hex map right now

    this.heapscore = 0; // for insertion into BinaryHeap

    // The following fields are specific to one query.
    // In order to avoid having to re-initialize the entire grid before each query,
    // we use a "mailbox" technique to do just-in-time initialization if serial mismatches.
    this.serial = 0;
    this.f = 0;
    this.g = 0;
    this.h = 0;
    this.visited = false;
    this.closed = false;
    this.parent = null;
};

// return true only if there is no data at all for this cell, meaning it can be deallocated
AStar.AStarCell.prototype.is_empty = function() { return this.block_count <= 0; }

AStar.AStarCell.prototype.block = function() { this.block_count += 1; }
AStar.AStarCell.prototype.unblock = function() { this.block_count -= 1; }

/** Test map cell for blockage. Optionally supply a "checker" function that can perform
 * arbitrary logic. Otherwise just uses block_count to determine blockage.
 * @param {AStar.BlockChecker=} checker
 * @return {boolean} */
AStar.AStarCell.prototype.is_blocked = function(checker) {
    if(checker) { return checker(this); }
    return this.block_count > 0;
}

/** Initialize fields needed by the A* search, lazily, and return reference to this.
 * @param {number} serial
 * @return {AStar.AStarCell} */
AStar.AStarCell.prototype.get = function(serial) {
    if(this.serial != serial) {
        this.f = 0;
        this.g = 0;
        this.h = 0;
        this.visited = false;
        this.closed = false;
        this.parent = null;
        this.serial = serial;
    }
    return this;
};

// 2D MAP - 2D GRID OF CELLS AND BLOCKAGE INFO

/** @constructor
  * @param {Array.<number>} size [x,y] size
  * @param {function(Array.<number>): boolean|null} terrain_func optional, return true if terrain is blocked at this location */
AStar.AStarMap = function (size, terrain_func) {
    this.size = size;
    this.terrain_func = terrain_func;
    this.needs_cleanup = false;
    this.generation = 0; // version number that increments with every mutation
    this.n_alloc = 0;
    /** @private */
    this.map = Array(size[1]); // columns of lazily-allocated rows of lazily-allocated AStarCells
    this.clear();

};

AStar.AStarMap.prototype.clear = function() {
    for(var y = 0; y < this.size[1]; y++) {
        this.map[y] = null;
    }
    this.needs_cleanup = false;
    this.n_alloc = 0;
    this.generation += 1;
}
AStar.AStarMap.prototype.cleanup = function() {
    if(!this.needs_cleanup) { return; }
    this.needs_cleanup = false;
    for(var y = 0; y < this.size[1]; y++) {
        if(this.map[y]) {
            var count = 0;
            for(var x = 0; x < this.size[0]; x++) {
                var c = this.map[y][x];
                if(c) {
                    if(c.is_empty()) {
                        this.map[y][x] = null;
                        this.n_alloc -= 1;
                    } else {
                        count += 1;
                    }
                }
            }
            if(count <= 0) {
                this.map[y] = null;
            }
        }
    }
}

/** Return a cell no matter what.
  * @param {Array.<number>} xy */
AStar.AStarMap.prototype.cell = function(xy) {
    if(!this.map[xy[1]]) {
        this.map[xy[1]] = Array(this.size[0]);
        for(var x = 0; x < this.size[0]; x++) {
            this.map[xy[1]][x] = null;
        }
    }
    if(!this.map[xy[1]][xy[0]]) {
        this.map[xy[1]][xy[0]] = new AStar.AStarCell(xy);
        this.n_alloc += 1;
        this.needs_cleanup = true;
    }
    return this.map[xy[1]][xy[0]];
};

/** @param {Array.<number>} xy */
AStar.AStarMap.prototype.free_cell = function(xy) {
    this.needs_cleanup = true;
    return;
};

/** Return a cell that can potentially be used for pathing
 * @param {Array.<number>} xy
 * @param {AStar.BlockChecker} checker
 * @return {?AStar.AStarCell} */
AStar.AStarMap.prototype.cell_if_unblocked = function(xy, checker) {
    if(xy[0] >= 0 && xy[0] < this.size[0] &&
       xy[1] >= 0 && xy[1] < this.size[1]) {
        if(this.terrain_func && this.terrain_func(xy)) { return null; }
        var c = this.cell(xy); // potentially create cell here if it is within the map area but not blocked
        if(c.is_blocked(checker)) { return null; }
        return c;
    }
    return null;
};

/** @param {Array.<number>} xy
 * @param {AStar.BlockChecker=} checker
 * @return {boolean} */
AStar.AStarMap.prototype.is_blocked = function(xy, checker) {
    if(xy[0] >= 0 && xy[0] < this.size[0] &&
       xy[1] >= 0 && xy[1] < this.size[1]) {
        if(this.terrain_func && this.terrain_func(xy)) { return true; }
        if(!this.map[xy[1]]) { return false; }
        var c = this.map[xy[1]][xy[0]];
        return c && c.is_blocked(checker);
    }
    return true;
};

AStar.AStarMap.prototype.num_neighbors = goog.abstractMethod;

/** @param {AStar.AStarCell} node
 * @param {AStar.BlockChecker} checker
 * @param {Array.<AStar.AStarCell>} ret */
AStar.AStarMap.prototype.get_unblocked_neighbors = goog.abstractMethod;

/** Iterate through all allocated cells, whether blocked or not. Used for integrity checking/debugging only.
 * @param {function(AStar.AStarCell)} func */
AStar.AStarMap.prototype.for_each_cell = function(func) {
   for(var y = 0; y < this.size[1]; y++) {
        if(this.map[y]) {
            for(var x = 0; x < this.size[0]; x++) {
                var cell = this.map[y][x];
                if(cell) {
                    func(cell);
                }
            }
        }
   }
};

// RECTANGULAR MAP

/** @constructor
 * @extends AStar.AStarMap
 * @param {Array.<number>} size [x,y] size
 * @param {function(Array.<number>): boolean|null} terrain_func optional, return true if terrain is blocked at this location
 * @param {boolean} allow_diagonal_passage if true, allow travel along "thin" Bresenham paths with diagonal moves
 */
AStar.AStarRectMap = function (size, terrain_func, allow_diagonal_passage) {
    goog.base(this, size, terrain_func);
    this.allow_diagonal_passage = allow_diagonal_passage;
};
goog.inherits(AStar.AStarRectMap, AStar.AStarMap);

/** @override */
AStar.AStarRectMap.prototype.num_neighbors = function() { return 4; };

/** @override */
AStar.AStarRectMap.prototype.get_unblocked_neighbors = function(node, checker, ret) {
    var x = node.pos[0];
    var y = node.pos[1];
    ret[0] = this.cell_if_unblocked([x-1,y], checker);
    ret[1] = this.cell_if_unblocked([x+1,y], checker);
    ret[2] = this.cell_if_unblocked([x,y-1], checker);
    ret[3] = this.cell_if_unblocked([x,y+1], checker);
};

/** update collision-detection data structure
 * @param {Array.<number>} xy upper-left corner of area to affect
 * @param {Array.<number>} wh width and height
 * @param {number} value +1 to block, -1 to unblock
 */
AStar.AStarRectMap.prototype.block_map = function(xy, wh, value) {
    for(var v = 0; v < wh[1]; v++) {
        for(var u = 0; u < wh[0]; u++) {
            var m = xy[1]+v, n = xy[0]+u;
            if(m >= 0 && m < this.size[1] && n >= 0 && n < this.size[0]) {
                var cell = this.cell([n,m]);
                if(cell) {
                    if(value > 0) {
                        cell.block();
                    } else if(value < 0) {
                        cell.unblock();
                    }
                    if(cell.is_empty()) {
                        this.free_cell([n,m]);
                    }
                }
            }
        }
    }
    this.generation += 1;
};

/** Return the list of [x,y] coordinates along a path from start to end, inclusive.
 * @param {Array.<number>} start
 * @param {Array.<number>} end
 * @returns {Array.<Array.<number>>} */
AStar.AStarRectMap.prototype.get_linear_path = function(start, end) {
    if(this.allow_diagonal_passage) {
        return this.get_bresenham_path(vec_floor(start), vec_floor(end));
    } else{
        return this.get_voxel_path(start, end);
    }
};

/** Bresenham's version - this allows diagonal movement, and only works on integer coordinates
 * @private
 * @param {Array.<number>} start
 * @param {Array.<number>} end
 * @returns {Array.<Array.<number>>} */
AStar.AStarRectMap.prototype.get_bresenham_path = function(start, end) {
    var dx = Math.abs(end[0]-start[0]), dy = Math.abs(end[1]-start[1]);
    var sx = (start[0] < end[0] ? 1 : -1), sy = (start[1] < end[1] ? 1 : -1);
    var err = dx - dy;
    var cur = [start[0], start[1]];
    var ret = [];
    var iter = 0;
    while(!(cur[0] == end[0] && cur[1] == end[1])) {
        ret.push([cur[0],cur[1]]);
        var e2 = 2*err;
        if(e2 > -dy) {
            err -= dy;
            cur[0] += sx;
        }
        if(e2 < dx) {
            err += dx;
            cur[1] += sy;
        }
        iter += 1;
        if(iter >= 10000) { throw Error('runaway iteration from '+start[0].toString()+','+start[1].toString()+' to '+end[0].toString()+','+end[1].toString()+' at '+cur[0].toString()+','+cur[1].toString()); }
    }
    ret.push([cur[0],cur[1]]);
    return ret;
};

/** Only horizontal or vertical movement allowed (no diagonal moves).
 *  Accepts continuous coordinates as input, and returns non-quantized grid cells.
 * @private
 * @param {Array.<number>} start
 * @param {Array.<number>} end
 * @returns {Array.<Array.<number>>} */
AStar.AStarRectMap.prototype.get_voxel_path = function(start, end) {
    // see http://www.cse.yorku.ca/~amana/research/grid.pdf

    var dir = vec_sub(end, start);
    var step = [(start[0] < end[0] ? 1 : -1),
                (start[1] < end[1] ? 1 : -1)];
    var delta = [0.0,0.0];
    var tmax = [0.0,0.0];
    for(var i = 0; i < 2; i++) {
        if(dir[i] > 0) {
            tmax[i] = (Math.floor(start[i]+1) - start[i])/dir[i];
            delta[i] = 1.0 / Math.abs(dir[i]);
        } else if(dir[i] < 0) {
            tmax[i] = (Math.floor(start[i]) - start[i])/dir[i];
            delta[i] = 1.0 / Math.abs(dir[i]);
        } else {
            tmax[i] = Infinity;
            delta[i] = 0;
        }
    }

    var cur = vec_copy(start);
    var ret = [];
    var iter = 0;
    while(!(Math.floor(cur[0]) == Math.floor(end[0]) && Math.floor(cur[1]) == Math.floor(end[1]))) {
        if(tmax[0] < tmax[1]) {
            // need extra checks to deal with rounding errors
            if(Math.floor(cur[0]) != Math.floor(end[0])) {
                ret.push(vec_copy(cur));
                cur[0] += step[0];
            }
            tmax[0] += delta[0];
        } else {
            if(Math.floor(cur[1]) != Math.floor(end[1])) {
                ret.push(vec_copy(cur));
                cur[1] += step[1];
            }
            tmax[1] += delta[1];
        }
        iter += 1;
        if(iter >= 10000) { throw Error('runaway iteration from '+start[0].toString()+','+start[1].toString()+' to '+end[0].toString()+','+end[1].toString()+' at '+cur[0].toString()+','+cur[1].toString()); }
    }
    ret.push(vec_copy(cur));
    return ret;
};

/** Return whether a straight-line path from xy coordinates 'start' to 'end' is free of collisions
 * @param {Array.<number>} start
 * @param {Array.<number>} end
 * @returns {boolean} */
AStar.AStarRectMap.prototype.linear_path_is_clear = function(start, end) {
    if(this.allow_diagonal_passage) {
        return this.bresenham_path_is_clear(vec_floor(start), vec_floor(end));
    } else {
        return this.voxel_path_is_clear(start, end);
    }
};

/** Bresenham's version - this allows diagonal movement, and only works on integer coordinates.
 *  Also, for compatibility with legacy games, this does NOT check the final endpoint cell.
 * @private
 * @param {Array.<number>} start
 * @param {Array.<number>} end
 * @returns {boolean} */
AStar.AStarRectMap.prototype.bresenham_path_is_clear = function(start, end) {
    // Bresenham's algorithm
    var dx = Math.abs(end[0]-start[0]), dy = Math.abs(end[1]-start[1]);
    var sx = (start[0] < end[0] ? 1 : -1), sy = (start[1] < end[1] ? 1 : -1);
    var err = dx - dy;
    var cur = [start[0], start[1]];
    var iter = 0;
    while(!(cur[0] == end[0] && cur[1] == end[1])) {
        if(this.is_blocked(cur)) {
            return false;
        }
        var e2 = 2*err;
        if(e2 > -dy) {
            err -= dy;
            cur[0] += sx;
        }
        if(e2 < dx) {
            err += dx;
            cur[1] += sy;
        }
        iter += 1;
        if(iter >= 10000) { throw Error('runaway iteration from '+start[0].toString()+','+start[1].toString()+' to '+end[0].toString()+','+end[1].toString()+' at '+cur[0].toString()+','+cur[1].toString()); }
    }
    // intentionally ignore final cell
    return true;
};

/** Only horizontal or vertical movement allowed (no diagonal moves).
 *  Accepts continuous coordinates as input.
 * @private
 * @param {Array.<number>} start
 * @param {Array.<number>} end
 * @returns {boolean} */
AStar.AStarRectMap.prototype.voxel_path_is_clear = function(start, end) {
    // see http://www.cse.yorku.ca/~amana/research/grid.pdf
    var dir = vec_sub(end, start);
    var step = [(start[0] < end[0] ? 1 : -1),
                (start[1] < end[1] ? 1 : -1)];
    var delta = [0,0];
    var tmax = [0,0];
    for(var i = 0; i < 2; i++) {
        if(dir[i] > 0) {
            tmax[i] = (Math.floor(start[i]+1) - start[i])/dir[i];
            delta[i] = 1 / Math.abs(dir[i]);
        } else if(dir[i] < 0) {
            tmax[i] = (Math.floor(start[i]) - start[i])/dir[i];
            delta[i] = 1 / Math.abs(dir[i]);
        } else {
            tmax[i] = Infinity;
            delta[i] = 0;
        }
    }

    var cur = vec_copy(start);
    var iter = 0;
    while(!(Math.floor(cur[0]) == Math.floor(end[0]) && Math.floor(cur[1]) == Math.floor(end[1]))) {
        if(this.is_blocked(vec_floor(cur))) {
            return false;
        }
        if(tmax[0] < tmax[1]) {
            // need extra checks to deal with rounding errors
            if(Math.floor(cur[0]) != Math.floor(end[0])) {
                cur[0] += step[0];
            }
            tmax[0] += delta[0];
        } else {
            if(Math.floor(cur[1]) != Math.floor(end[1])) {
                cur[1] += step[1];
            }
            tmax[1] += delta[1];
        }
        iter += 1;
        if(iter >= 10000) { throw Error('runaway iteration from '+start[0].toString()+','+start[1].toString()+' to '+end[0].toString()+','+end[1].toString()+' at '+cur[0].toString()+','+cur[1].toString()); }
    }
    if(this.is_blocked(vec_floor(cur))) {
        return false;
    }
    return true;
};

/** smooth a "manhattan-aliased" path by removing as many nodes as
 * possible without creating any collisions
 * note: modifies 'path' destructively
 * @param {Array.<Array.<number>>} path
 */
AStar.AStarRectMap.prototype.smooth_path = function(path) {
    if(path.length < 2) { return path; }

    /*
      console.log('starting path: ');
      var str = '';
      for(var x = 0; x < path.length; x++) { str += ' ['+path[x].pos[0]+','+path[x].pos[1]+']'; }
      console.log(str);
    */

    var start_pos = path[0];
    var i = 1;
    while(i < path.length-1) {
        // check path from start_pos to the node AFTER i
        var next_pos = path[i+1];
        if(this.linear_path_is_clear(start_pos, next_pos)) {
            // remove node i from path
            path.splice(i, 1);
        } else {
            start_pos = path[i];
            i += 1;
        }
    }

    /*
      console.log('final path');
      str = '';
      for(var x = 0; x < path.length; x++) { str += ' ['+path[x].pos[0]+','+path[x].pos[1]+']'; }
      console.log(str);
    */

    return path;
};

// HEX MAP

/** @constructor
    @extends AStar.AStarMap */
AStar.AStarHexMap = function (size, terrain_func) {
    goog.base(this, size, terrain_func);
};
goog.inherits(AStar.AStarHexMap, AStar.AStarMap);

/** @override */
AStar.AStarHexMap.prototype.num_neighbors = function() { return 6; };

/** @override */
AStar.AStarHexMap.prototype.get_unblocked_neighbors = function(node, checker, ret) {
    var x = node.pos[0], y = node.pos[1];
    var odd = (y%2) > 0;

    ret[0] = this.cell_if_unblocked([x-1,y], checker); // left
    ret[1] = this.cell_if_unblocked([x+1,y], checker); // right
    ret[2] = this.cell_if_unblocked([x+odd-1,y-1], checker); // upper-left
    ret[3] = this.cell_if_unblocked([x+odd,y-1], checker); // upper-right
    ret[4] = this.cell_if_unblocked([x+odd-1,y+1], checker); // lower-left
    ret[5] = this.cell_if_unblocked([x+odd,y+1], checker); // lower-right
};

/** block/unblock individual hexes
    @param {Array.<number>} xy
    @param {number} value +1 to block, -1 to unblock
    @param {!Object} blocker reference to thing that is blocking here
*/
AStar.AStarHexMap.prototype.block_hex = function(xy, value, blocker) {
    if(!blocker) { throw Error('must provide blocker'); } // Closure doesn't flag this
    if(xy[0] >= 0 && xy[0] < this.size[0] && xy[1] >= 0 && xy[1] < this.size[1]) {
        var cell = this.cell(xy);
        if(cell) {
            if(value > 0) {
                cell.block();
                if(cell.blockers === null) {
                    cell.blockers = [blocker];
                } else {
                    cell.blockers.push(blocker);
                }
            } else if(value < 0) {
                cell.unblock();
                if(!cell.blockers || !goog.array.remove(cell.blockers, blocker)) {
                    throw Error('unblock hex but blocker not found: '+JSON.stringify(blocker)+' in '+JSON.stringify(cell.blockers));
                }
                if(cell.blockers.length == 0) {
                    cell.blockers = null;
                }
            }
            if(cell.is_empty()) {
                this.free_cell(xy);
            }
        }
    }
    this.generation += 1;
};

/** unblock a hex, but only if "blocker" is currently blocking it
    @param {Array.<number>} xy
    @param {!Object} blocker reference to thing that is blocking here
*/
AStar.AStarHexMap.prototype.unblock_hex_maybe = function(xy, blocker) {
    if(this.is_blocked(xy)) {
        var cell = this.cell(xy);
        if(cell.blockers && goog.array.contains(cell.blockers, blocker)) {
            this.block_hex(xy, -1, blocker);
        }
    }
};

// Connectivity stores a "region number" for each cell in the map,
// based on the map's blockage at instantiation time. (it does NOT update itself).
// "region numbers" for two cells are the same if and only if there
// is an unblocked path between them. Blocked cells get "region number" -1.

/** @constructor
 * @param {AStar.AStarMap} map
 */
AStar.Connectivity = function(map) {
    if(!(map instanceof AStar.AStarRectMap)) { throw Error('Connectivity only implemented for RectMap'); }

    /** @type {Array.<number>} */
    this.size = [map.size[0], map.size[1]];

    /** @type {Array.<number>} */
    this.flood = new Array(this.size[0]*this.size[1]);

    // use a flood-fill algorithm to assign region numbersj

    var val = 0;
    for(var y = 0; y < this.size[1]; y++) {
        for(var x = 0; x < this.size[0]; x++) {
            if(typeof(this.flood[y*this.size[1]+x]) != 'undefined') {
                continue;
            }
            if(map.is_blocked([x,y])) {
                this.flood[y*this.size[1]+x] = -1;
                continue;
            }
            val += 1;
            var q = [[x,y]];
            while(q.length > 0) {
                var p = q.pop();
                if(typeof(this.flood[p[1]*this.size[1]+p[0]]) != 'undefined') {
                    continue;
                }
                if(map.is_blocked(p)) {
                    this.flood[y*this.size[1]+x] = -1;
                    continue;
                } else {
                    // note: this only works for rectangular maps at the moment, but could be adapted to a hex map if necessary
                    var w = p[0], e = p[0];
                    while(!map.is_blocked([w,p[1]]) && typeof(this.flood[p[1]*this.size[1]+w]) == 'undefined' && w >= 0) {
                        w -= 1;
                    }
                    w += 1;
                    while(!map.is_blocked([e,p[1]]) && typeof(this.flood[p[1]*this.size[1]+e]) == 'undefined' && e < this.size[0]) {
                        e += 1;
                    }
                    e -= 1;
                    for(var x2 = w; x2 <= e; x2++) {
                        this.flood[p[1]*this.size[1]+x2] = val;
                        if(p[1] > 0 && !map.is_blocked([x2,p[1]-1]) && typeof(this.flood[(p[1]-1)*this.size[1]+x2]) == 'undefined') {
                            q.push([x2,p[1]-1]);
                        }
                        if(p[1] < this.size[1]-1 && !map.is_blocked([x2,p[1]+1]) && typeof(this.flood[(p[1]+1)*this.size[1]+x2]) == 'undefined') {
                            q.push([x2,p[1]+1]);
                        }
                    }
                }
            }
        }
    }
};

AStar.Connectivity.prototype.region_num = function(pos) {
    return this.flood[pos[1]*this.size[1]+pos[0]];
};

AStar.Connectivity.prototype.debug_draw = function() {
    ctx.save();
    for(var y = 0; y < this.size[1]; y++) {
        for(var x = 0; x < this.size[0]; x++) {
            var val = this.flood[y*this.size[1]+x];
            if(val < 0) { continue; }
            var col = (val*0.20);
            var col_str = new SPUI.Color(col,col,col,1).str();
            ctx.fillStyle = col_str;
            var xy = ortho_to_draw([x+0.5,y+0.5]);
            ctx.fillRect(xy[0], xy[1], 4, 4);
        }
    }
    ctx.restore();
};

// SEARCH CONTEXT

/** @constructor
    @param {AStar.AStarMap} map
    @param {{heuristic_name:(string|undefined),
             iter_limit:(number|undefined),
             use_connectivity:(boolean|undefined)
    }} options */
AStar.AStarContext = function(map, options) {
    this.options = options;

    // serial number for cell mailboxes
    this.serial = 1;
    this.map = map;
    this.iter_limit = options.iter_limit || -1;
    // "scene graph" for debug drawing
    this.debug_scene = [];

    var heuristic_name = options.heuristic_name || 'manhattan';
    if(heuristic_name === 'manhattan') {
        this.heuristic = AStar.heuristic_manhattan;
    } else if(heuristic_name === 'euclidean') {
        this.heuristic = AStar.heuristic_euclidean;
    } else {
        throw Error('unknown A* heuristic '+heuristic_name);
    }
};

AStar.AStarContext.prototype.debug_clear = function() {
    this.debug_scene = [];
};

AStar.AStarContext.prototype.debug_draw = function(ctx) {
    // draw dots on the landscape
    ctx.save();
    goog.array.forEach(this.debug_scene, function(item) {
        var pos = item[0], col_str = item[1];
        var xy = ortho_to_draw(vec_add(pos, [0.5,0.5]));
        ctx.fillStyle = col_str;
        ctx.fillRect(xy[0], xy[1], 4, 4);
    });
    ctx.restore();
};

/** Main A* search function
  * @param {Array.<number>} start_pos
  * @param {Array.<number>} end_pos
  * @param {AStar.PathChecker=} path_checker
  * @return {Array.<Array.<number>>}
  */
AStar.AStarContext.prototype.search = function(start_pos, end_pos, path_checker) {
    path_checker = path_checker || null;
    this.serial += 1;
    if(PLAYFIELD_DEBUG) { this.debug_clear(); }

    var start = this.map.cell(start_pos), end = this.map.cell(end_pos);

    if(!start || !end) { return []; } // to or from cell that's not in the map

    // note: assume that all nodes pushed onto the openHeap already have been initialized via get()
    // sort by node.f
    var openHeap = new BinaryHeap();

    openHeap.push(start, start.get(this.serial).f);

    var iter = 0;

    // keep track of closest node to endpoint, so that we can
    // return a partial path in case all complete paths are blocked
    /** @type {AStar.AStarCell} */
    var best_node = null;
    var best_h = 9999999999;

    // preallocate for speed
    /** @type {Array.<AStar.AStarCell>} */
    var neighbors = Array(this.map.num_neighbors());

    while(openHeap.size() > 0 && iter < AStar.ASTAR_MAX_ITER) {
        iter +=1;
        if(iter >= AStar.ASTAR_MAX_ITER) {
            throw Error('infinite loop in astar.search()!');
        } else if(this.iter_limit > 0 && iter >= this.iter_limit) {
            break; // give up
        }

        /*
          if(ASTAR_DEBUG) {
          console.log('HEAP: ');
          for(var i = 0; i < openHeap.content.length; i++) {
          console.log(openHeap.content[i].pos + ' F ' + openHeap.content[i].f + ' G ' + openHeap.content[i].g + ' H ' + openHeap.content[i].h + ' visited ' + openHeap.content[i].visited + ' parent ' + (openHeap.content[i].parent && openHeap.content[i].parent.pos));
          }
          }
        */

        // Grab the lowest f(x) to process next.  Heap keeps this sorted for us.
        var currentNode = openHeap.pop();

        // accumulate dots to draw into the scene graph
        if(PLAYFIELD_DEBUG) {
            var col = (iter % 10) / 10;
            var col_str = new SPUI.Color(col,col,col,1).str();
            this.debug_scene.push([currentNode.pos, col_str]);
        }

        // End case -- result has been found, return the traced path
        if(currentNode === end) {
            if(AStar.ASTAR_DEBUG) {
                console.log('AStar.search DONE at ' + currentNode.pos[0].toString()+','+currentNode.pos[1].toString()+' iter '+iter.toString());
            }
            var curr = currentNode;
            var ret = [];

            while(curr.parent != null) {
                ret.push(curr.pos);
                curr = curr.parent;
            }
            return ret.reverse();
        }

        // Normal case -- move currentNode from open to closed, process each of its neighbors
        currentNode.get(this.serial).closed = true;

        /** wrap path_checker into a version that is_blocked() can call with only the current cell as the argument
         * @type {AStar.BlockChecker | null} */
        var cell_checker = null;
        if(path_checker) {
            var cur_path = [];
            for(var c = currentNode; c.parent != null; c = c.parent) {
                cur_path.push(c.pos);
            }
            cur_path = cur_path.reverse();
            cell_checker = (function (_path_checker, _cur_path) { return function(cell) { return _path_checker(cell, _cur_path.concat(cell.pos)); }; })(path_checker, cur_path);
        }

        // get references to neighbor cells
        this.map.get_unblocked_neighbors(currentNode, cell_checker, neighbors);

        for(var i = 0, il = neighbors.length; i < il; i++) {
            var neighbor = neighbors[i];

            if(!neighbor || neighbor.is_blocked(cell_checker) || neighbor.get(this.serial).closed) {
                // not a valid node to process, skip to next neighbor
                continue;
            }

            // g score is the shortest distance from start to current node, we need to check if
            //   the path we have arrived at this neighbor is the shortest one we have seen yet
            // 1 is the distance from a node to it's neighbor.  This could be variable for weighted paths.
            var gScore = currentNode.g + 1;
            var beenVisited = neighbor.visited;

            if(!beenVisited || (gScore < neighbor.g)) {
                // Found an optimal (so far) path to this node.  Take score for node to see how good it is.
                neighbor.visited = true;
                neighbor.parent = currentNode;
                /*
                  if(AStar.ASTAR_DEBUG) {
                  console.log('set parent of '+neighbor.pos+' to '+currentNode.pos + ' beenVisited ' + beenVisited + ' gScore ' + gScore + ' neighbor.g ' + neighbor.astar_data.g);
                  }
                */
                neighbor.h = neighbor.h || this.heuristic(start.pos, neighbor.pos, end.pos);
                neighbor.g = gScore;
                neighbor.f = neighbor.g + neighbor.h;

                // keep track of "best" node seen so far, in case we fail to find a path all the way to the target
                if(neighbor.h < best_h) {
                    best_h = neighbor.h;
                    best_node = neighbor;
                }

                if (!beenVisited) {
                    // Pushing to heap will put it in proper place based on the 'f' value.
                    openHeap.push(neighbor, neighbor.f);
                } else {
                    // Already seen the node, but since it has been rescored we need to reorder it in the heap
                    openHeap.rescoreElement(neighbor, neighbor.f);
                }
            }
        }
    }

    // No result was found -- empty array signifies failure to find path
    var ret = [];
    if(1 && (best_node != null)) {
        var curr = best_node;
        while(curr.parent != null) {
            ret.push(curr.pos);
            curr = curr.parent;
        }
        ret = ret.reverse();
    }
    if(AStar.ASTAR_DEBUG && iter >= (0.25*this.map.size[0]*this.map.size[1])) {
        var msg = 'pathological AStar query from '+start_pos[0].toString()+','+start_pos[1].toString()+' to '+end_pos[0].toString()+','+end_pos[1].toString()+' iter '+iter.toString()
        if(AStar.ASTAR_DEBUG >= 3) {
            throw Error(msg);
        } else {
            console.log(msg);
        }
    }
    return ret;
};

/** Cached wrapper for AStarContext that memoizes previous queries and optionally caches connectivity.
 * @constructor
 * @extends AStar.AStarContext */
AStar.CachedAStarContext = function(map, options) {
    goog.base(this, map, options);
    this.cache_generation = -1;
    this.cache = {};

    /** @type {AStar.Connectivity|null} */
    this.connectivity = null;
};
goog.inherits(AStar.CachedAStarContext, AStar.AStarContext);

// if map generation has advanced, clear all caches
AStar.CachedAStarContext.prototype.check_dirty = function() {
    if(this.map.generation !== this.cache_generation) {
        this.cache_generation = this.map.generation;
        this.cache = {};
        if(this.connectivity) { this.connectivity = null; }
    }
};
AStar.CachedAStarContext.prototype.ensure_connectivity = function() {
    if(!this.connectivity && this.options.use_connectivity) {
        this.connectivity = new AStar.Connectivity(this.map);
    }
};
AStar.CachedAStarContext.prototype.debug_draw = function(ctx) {
    if(this.connectivity) {
        this.connectivity.debug_draw();
    } else {
        return goog.base(this, 'debug_draw', ctx);
    }
};

/**
 * @param {Array.<number>} start_pos
 * @param {Array.<number>} end_pos
 * @param {number} ring_size
 */
AStar.CachedAStarContext.prototype.cache_key = function(start_pos, end_pos, ring_size) {
    return (start_pos[0].toFixed(0)+','+start_pos[1].toFixed(0)+':'+end_pos[0].toFixed(0)+','+end_pos[1].toFixed(0)+','+ring_size.toFixed(0));
};

/** Cached wrapper around A* search function
 * @override
 * @param {Array.<number>} start_pos
 * @param {Array.<number>} end_pos
 * @param {AStar.PathChecker=} path_checker
 * @return {Array.<Array.<number>>}
 */
AStar.CachedAStarContext.prototype.search = function(start_pos, end_pos, path_checker) {
    if(path_checker) { throw Error('checkers not supported in CachedAStarContext'); }
    this.check_dirty();

    var key = this.cache_key(start_pos, end_pos, 0);
    if(key in this.cache) {
        // must make a copy, because the caller may mutate the path
        return goog.array.clone(this.cache[key]);
    }

    this.ensure_connectivity();

    var start_region = (this.connectivity ? this.connectivity.region_num(start_pos) : 0);
    var end_region = (this.connectivity ? this.connectivity.region_num(end_pos) : 0);

    /** @type {Array.<Array.<number>>} */
    var ret;

    if(start_region < 0 || start_region != end_region) {
        ret = []; // early out - no connection
        // XXX eventually want to check end_region < 0 here, but callers are still looking for paths into blocked cells
    } else {
        ret = goog.base(this, 'search', start_pos, end_pos, path_checker);
    }

    // must make a copy, because the caller may mutate the path
    this.cache[key] = goog.array.clone(ret);
    return ret;
};

/** Search towards a point in an expanding ring of up to 'ring_size' map cells
  * @param {Array.<number>} start_pos
  * @param {Array.<number>} end_pos
  * @param {number} ring_size
  * @return {Array.<Array.<number>>}
  */
AStar.CachedAStarContext.prototype.ring_search = function(start_pos, end_pos, ring_size) {
    if(ring_size < 1) { throw Error('ring_size < 1'); }
    this.check_dirty();

    var key = this.cache_key(start_pos, end_pos, ring_size);
    if(key in this.cache) {
        // must make a copy, because the caller may mutate the path
        return goog.array.clone(this.cache[key]);
    }

    this.ensure_connectivity();

    var start_region = (this.connectivity ? this.connectivity.region_num(start_pos) : 0);

    /** @type {Array.<Array.<number>>|null} */
    var ret = null;

    if(start_region < 0) {
        ret = []; // search from inside blocked area
    } else {
        for(var r = 1; r <= ring_size; r++) {
            var points = [];

            // iterate on the ring of radius r
            for(var x = end_pos[0]-r; x <= end_pos[0]+r; x++) {
                for(var y = end_pos[1]-r; y <= end_pos[1]+r; y += ((x == end_pos[0]-r || x == end_pos[0]+r) ? 1 : 2*r)) {
                    if(x < 0 || x >= this.map.size[0] || y < 0 || y >= this.map.size[1]) { continue; }
                    var p = [x,y];
                    var end_region = (this.connectivity ? this.connectivity.region_num(p) : 0);
                    if(end_region == start_region && end_region >= 0) { // only consider if possibly accessible
                        if(!this.map.is_blocked(p)) {
                            points.push(p);
                        }
                    }
                }
            }

            if(points.length < 1) { continue; }
            points.sort(function (a,b) {
                var da = vec_distance(a, start_pos);
                var db = vec_distance(b, start_pos);
                if(da > db) {
                    return 1;
                } else if(da < db) {
                    return -1;
                } else {
                    return 0;
                }
            });
            for(var i = 0; i < points.length; i++) {
                var path = this.search(start_pos, points[i]); // note: this uses the cache as well
                if(path) {
                    ret = path;
                    break;
                }
            }
            if(ret !== null) { break; }
        }
    }
    if(ret === null) { // no path
        ret = [];
    }

    // must make a copy, because the caller may mutate the path
    this.cache[key] = goog.array.clone(ret);
    return ret;
};
