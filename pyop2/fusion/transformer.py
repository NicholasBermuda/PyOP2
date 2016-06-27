# This file is part of PyOP2
#
# PyOP2 is Copyright (c) 2016, Imperial College London and
# others. Please see the AUTHORS file in the main source directory for
# a full list of copyright holders.  All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * The name of Imperial College London or that of other
#       contributors may not be used to endorse or promote products
#       derived from this software without specific prior written
#       permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTERS
# ''AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDERS OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
# INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
# OF THE POSSIBILITY OF SUCH DAMAGE.

"""Core loop fusion mechanisms."""

import os
from collections import OrderedDict, namedtuple
from copy import deepcopy as dcopy

from pyop2.base import READ, RW, WRITE, MIN, MAX, INC, _LazyMatOp, IterationIndex, \
    Subset, Map
from pyop2.mpi import MPI
from pyop2.caching import Cached
from pyop2.profiling import timed_region
from pyop2.utils import flatten, as_tuple
from pyop2.logger import warning
from pyop2 import compilation

from extended import lazy_trace_name, Kernel
from filters import Filter, WeakFilter
from interface import slope
from scheduler import *

import coffee
from coffee import base as ast
from coffee.utils import ItSpace
from coffee.visitors import FindInstances, SymbolReferences


class Inspector(Cached):

    """An Inspector constructs a Schedule to fuse or tile a sequence of loops.

    .. note:: For tiling, the Inspector relies on the SLOPE library."""

    _cache = {}
    _modes = ['soft', 'hard', 'tile', 'only_tile', 'only_omp']

    @classmethod
    def _cache_key(cls, name, loop_chain, **options):
        key = (name,)
        if name != lazy_trace_name:
            # Special case: the Inspector comes from a user-defined /loop_chain/
            key += (options['mode'], options['tile_size'],
                    options['use_glb_maps'], options['use_prefetch'], options['coloring'])
            key += (loop_chain[0].kernel.cache_key,)
            return key
        # Inspector extracted from lazy evaluation trace
        for loop in loop_chain:
            if isinstance(loop, _LazyMatOp):
                continue
            key += (loop.kernel.cache_key,)
            key += (loop.it_space.cache_key, loop.it_space.iterset.sizes)
            for arg in loop.args:
                if arg._is_global:
                    key += (arg.data.dim, arg.data.dtype, arg.access)
                elif arg._is_dat:
                    if isinstance(arg.idx, IterationIndex):
                        idx = (arg.idx.__class__, arg.idx.index)
                    else:
                        idx = arg.idx
                    map_arity = arg.map.arity if arg.map else None
                    key += (arg.data.dim, arg.data.dtype, map_arity, idx, arg.access)
                elif arg._is_mat:
                    idxs = (arg.idx[0].__class__, arg.idx[0].index,
                            arg.idx[1].index)
                    map_arities = (arg.map[0].arity, arg.map[1].arity)
                    key += (arg.data.dims, arg.data.dtype, idxs, map_arities, arg.access)
        return key

    def __init__(self, name, loop_chain, **options):
        """Initialize an Inspector object.

        :arg name: a name for the Inspector
        :arg loop_chain: an iterator for the loops that will be fused/tiled
        :arg options: a set of parameters to drive fusion/tiling
            * mode: can take any of the values in ``Inspector._modes``, namely
                soft, hard, tile, only_tile, only_omp:
                * soft: consecutive loops over the same iteration set that do
                    not present RAW or WAR dependencies through indirections
                    are fused.
                * hard: ``soft`` fusion; then, loops over different iteration sets
                    are also fused, provided that there are no RAW or WAR
                    dependencies.
                * tile: ``soft`` and ``hard`` fusion; then, tiling through the
                    SLOPE library takes place.
                * only_tile: only tiling through the SLOPE library (i.e., no fusion)
                * only_omp: ompize individual parloops through the SLOPE library
            * tile_size: starting average tile size
            * extra_halo: are we providing SLOPE with extra halo to be efficient
                and allow it to minimize redundant computation ?
        """
        if self._initialized:
            return
        self._name = name
        self._loop_chain = loop_chain
        self._mode = options.pop('mode')
        self._options = options
        self._schedule = PlainSchedule(name, [loop.kernel for loop in self._loop_chain])

    def inspect(self):
        """Inspect the loop chain and produce a :class:`Schedule`."""
        if self._initialized:
            # An inspection plan is in cache.
            return self._schedule
        elif self._heuristic_skip_inspection():
            # Not in cache, and too premature for running a potentially costly inspection
            del self._name
            del self._loop_chain
            del self._mode
            del self._options
            return self._schedule

        # Is `mode` legal ?
        if self.mode not in Inspector._modes:
            raise RuntimeError("Inspection accepts only %s fusion modes", Inspector._modes)

        with timed_region("ParLoopChain `%s`: inspector" % self._name):
            if self.mode in ['soft', 'hard', 'tile']:
                self._soft_fuse()
            if self.mode in ['hard', 'tile']:
                self._hard_fuse()
            if self.mode in ['tile', 'only_tile', 'only_omp']:
                self._tile()

        # A schedule has been computed. The Inspector is initialized and therefore
        # retrievable from cache. We then blow away everything we don't need any more.
        self._initialized = True
        del self._name
        del self._loop_chain
        del self._mode
        del self._options
        return self._schedule

    def _heuristic_skip_inspection(self):
        """Decide, heuristically, whether to run an inspection or not.
        If tiling is not requested, then inspection is performed.
        If tiling is requested, then inspection is performed on the third
        invocation. The fact that an inspection for the same loop chain
        is requested multiple times suggests the parloops originate in a
        time stepping loop. The cost of building tiles in SLOPE-land would
        then be amortized over several iterations."""
        self._ninsps = self._ninsps + 1 if hasattr(self, '_ninsps') else 1
        if self.mode in ['tile', 'only_tile'] and self._ninsps < 3:
            return True
        return False

    def _soft_fuse(self):
        """Fuse consecutive loops over the same iteration set by concatenating
        kernel bodies and creating new :class:`ParLoop` objects representing
        the fused sequence.

        The conditions under which two loops over the same iteration set can
        be soft fused are:

            * They are both direct, OR
            * One is direct and the other indirect

        This is detailed in the paper::

            "Mesh Independent Loop Fusion for Unstructured Mesh Applications"

        from C. Bertolli et al.
        """

        loop_chain = self._loop_chain

        handled, fusing = [], [loop_chain[0]]
        for i, loop in enumerate(loop_chain[1:]):
            base_loop = fusing[-1]
            info = loops_analyzer(base_loop, loop)
            if info['heterogeneous'] or info['indirect_w']:
                # Cannot fuse /loop/ into /base_loop/, so fuse what we found to be
                # fusible so far and pick a new base
                fused_kernel = build_soft_fusion_kernel(fusing, len(handled))
                handled.append((fused_kernel, i+1))
                fusing = [loop]
            else:
                # /base_loop/ and /loop/ are fusible. Before fusing them, we
                # speculatively search for more loops to fuse
                fusing.append(loop)
        if fusing:
            # Remainder
            fused_kernel = build_soft_fusion_kernel(fusing, len(handled))
            handled.append((fused_kernel, len(loop_chain)))

        self._schedule = FusionSchedule(self._name, self._schedule, *zip(*handled))
        self._loop_chain = self._schedule(loop_chain)

    def _hard_fuse(self):
        """Fuse consecutive loops over different iteration sets that do not
        present RAW, WAR or WAW dependencies. For examples, two loops like: ::

            par_loop(kernel_1, it_space_1,
                     dat_1_1(INC, ...),
                     dat_1_2(READ, ...),
                     ...)

            par_loop(kernel_2, it_space_2,
                     dat_2_1(INC, ...),
                     dat_2_2(READ, ...),
                     ...)

        where ``dat_1_1 == dat_2_1`` and, possibly (but not necessarily),
        ``it_space_1 != it_space_2``, can be hard fused. Note, in fact, that
        the presence of ``INC`` does not imply a real WAR dependency, because
        increments are associative."""

        loop_chain = self._loop_chain

        # Search pairs of hard-fusible loops
        fusible = []
        base_loop_index = 0
        while base_loop_index < len(loop_chain):
            base_loop = loop_chain[base_loop_index]

            for i, loop in enumerate(loop_chain[base_loop_index+1:], 1):
                info = loops_analyzer(base_loop, loop)

                if info['homogeneous']:
                    # Hard fusion is meaningless if same iteration space
                    continue

                if not info['pure_iai']:
                    # Can't fuse across loops presenting RAW or WAR dependencies
                    break

                base_inc_dats = set(a.data for a in incs(base_loop))
                loop_inc_dats = set(a.data for a in incs(loop))
                common_inc_dats = base_inc_dats | loop_inc_dats
                common_incs = [a for a in incs(base_loop) | incs(loop)
                               if a.data in common_inc_dats]
                if not common_incs:
                    # Is there an overlap in any of the incremented dats? If
                    # that's not the case, fusion is fruitless
                    break

                # Hard fusion requires a map between the iteration spaces involved
                maps = set(a.map for a in common_incs if a._is_indirect)
                maps |= set(flatten(m.factors for m in maps if hasattr(m, 'factors')))
                set1, set2 = base_loop.it_space.iterset, loop.it_space.iterset
                fusion_map_1 = [m for m in maps if set1 == m.iterset and set2 == m.toset]
                fusion_map_2 = [m for m in maps if set1 == m.toset and set2 == m.iterset]
                if fusion_map_1:
                    fuse_loop = loop
                    fusion_map = fusion_map_1[0]
                elif fusion_map_2:
                    fuse_loop = base_loop
                    base_loop = loop
                    fusion_map = fusion_map_2[0]
                else:
                    continue

                if any(a._is_direct for a in fuse_loop.args):
                    # Cannot perform direct reads in a /fuse/ kernel
                    break

                common_inc = [a for a in common_incs if a in base_loop.args][0]
                fusible.append((base_loop, fuse_loop, fusion_map, common_inc))
                break

            # Set next starting point of the search
            base_loop_index += i

        # For each pair of hard-fusible loops, create a suitable Kernel
        fused = []
        for base_loop, fuse_loop, fusion_map, fused_inc_arg in fusible:
            loop_chain_index = (loop_chain.index(base_loop), loop_chain.index(fuse_loop))
            fused_kernel, fargs = build_hard_fusion_kernel(base_loop, fuse_loop,
                                                           fusion_map, loop_chain_index)
            fused.append((fused_kernel, fusion_map, fargs))

        # Finally, generate a new schedule
        self._schedule = HardFusionSchedule(self._name, self._schedule, fused)
        self._loop_chain = self._schedule(loop_chain, only_hard=True)

    def _tile(self):
        """Tile consecutive loops over different iteration sets characterized
        by RAW and WAR dependencies. This requires interfacing with the SLOPE
        library."""

        loop_chain = self._loop_chain
        tile_size = self._options.get('tile_size', 1)
        extra_halo = self._options.get('extra_halo', False)
        coloring = self._options.get('coloring', 'default')
        use_prefetch = self._options.get('use_prefetch', 0)
        log = self._options.get('log', False)
        rank = MPI.COMM_WORLD.rank

        # SLOPE MPI backend unsupported if extra halo not available
        if slope.get_exec_mode() in ['OMP_MPI', 'ONLY_MPI'] and \
                not all(hasattr(l.it_space.iterset, '_deep_size') for l in loop_chain):
            warning("Tiling through SLOPE requires deep halos in all PyOP2 sets.")
            return

        # The SLOPE inspector, which needs be populated with sets, maps,
        # descriptors, and loop chain structure
        inspector = slope.Inspector(self._name)

        # Build inspector and argument types and values
        # Note: we need ordered containers to be sure that SLOPE generates
        # identical code for all ranks
        arguments = []
        insp_sets, insp_maps, insp_loops = OrderedDict(), OrderedDict(), []
        for loop in loop_chain:
            slope_desc = set()
            # 1) Add sets
            iterset = loop.it_space.iterset
            iterset = iterset.subset if hasattr(iterset, 'subset') else iterset
            slope_set = create_slope_set(iterset, extra_halo, insp_sets)
            # If iterating over a subset, we fake an indirect parloop from the
            # (iteration) subset to the superset. This allows the propagation of
            # tiling across the hierarchy of sets (see SLOPE for further info)
            if slope_set.superset:
                create_slope_set(iterset.superset, extra_halo, insp_sets)
                map_name = "%s_tosuperset" % slope_set.name
                insp_maps[slope_set.name] = (map_name, slope_set.name,
                                             iterset.superset.name, iterset.indices)
                slope_desc.add((map_name, INC._mode))
            for a in loop.args:
                # 2) Add access descriptors
                maps = as_tuple(a.map, Map)
                if not maps:
                    # Simplest case: direct loop
                    slope_desc.add(('DIRECT', a.access._mode))
                else:
                    # Add maps (there can be more than one per argument if the arg
                    # is actually a Mat - in which case there are two maps - or if
                    # a MixedMap) and relative descriptors
                    for i, map in enumerate(maps):
                        for j, m in enumerate(map):
                            map_name = "%s%d_%d" % (m.name, i, j)
                            insp_maps[m.name] = (map_name, m.iterset.name,
                                                 m.toset.name, m.values_with_halo)
                            slope_desc.add((map_name, a.access._mode))
                            create_slope_set(m.iterset, extra_halo, insp_sets)
                            create_slope_set(m.toset, extra_halo, insp_sets)
            # 3) Add loop
            insp_loops.append((loop.kernel.name, slope_set.name, list(slope_desc)))
        # Provide structure of loop chain to SLOPE
        arguments.extend([inspector.add_sets(insp_sets.keys())])
        arguments.extend([inspector.add_maps(insp_maps.values())])
        inspector.add_loops(insp_loops)

        # Set a specific tile size
        arguments.extend([inspector.set_tile_size(tile_size)])

        # Tell SLOPE the rank of the MPI process
        arguments.extend([inspector.set_mpi_rank(rank)])

        # Get type and value of additional arguments that SLOPE can exploit
        arguments.extend(inspector.add_extra_info())

        # Add any available partitioning
        partitionings = [(s[0], v) for s, v in insp_sets.items() if v is not None]
        arguments.extend([inspector.add_partitionings(partitionings)])

        # Arguments types and values
        argtypes, argvalues = zip(*arguments)

        # Set a tile partitioning strategy
        inspector.set_part_mode('chunk')

        # Set a tile coloring strategy
        inspector.set_coloring(coloring)

        # Inform about the prefetch distance that needs be guaranteed
        inspector.set_prefetch_halo(use_prefetch)

        # Generate the C code
        src = inspector.generate_code()

        # Return type of the inspector
        rettype = slope.Executor.meta['py_ctype_exec']

        # Compiler and linker options
        slope_dir = os.environ['SLOPE_DIR']
        compiler = coffee.system.compiler.get('name')
        cppargs = slope.get_compile_opts(compiler)
        cppargs += ['-I%s/%s' % (slope_dir, slope.get_include_dir())]
        ldargs = ['-L%s/%s' % (slope_dir, slope.get_lib_dir()),
                  '-l%s' % slope.get_lib_name(),
                  '-Wl,-rpath,%s/%s' % (slope_dir, slope.get_lib_dir()),
                  '-lrt']

        # Compile and run inspector
        fun = compilation.load(src, "cpp", "inspector", cppargs, ldargs,
                               argtypes, rettype, compiler)
        inspection = fun(*argvalues)

        # Log the inspector output
        if log and rank == 0:
            filename = os.path.join("log", "%s.txt" % self._name)
            summary = os.path.join("log", "summary.txt")
            if not os.path.exists(os.path.dirname(filename)):
                os.makedirs(os.path.dirname(filename))
            with open(filename, 'w') as f, open(summary, 'a') as s:
                # Estimate tile footprint
                template = '| %25s | %22s | %-11s |\n'
                f.write('*** Tile footprint ***\n')
                f.write(template % ('iteration set', 'memory footprint (KB)', 'megaflops'))
                f.write('-' * 68 + '\n')
                tot_footprint, tot_flops = 0, 0
                for loop in loop_chain:
                    flops, footprint = loop.num_flops/(1000*1000), 0
                    for arg in loop.args:
                        dat_size = arg.data.nbytes
                        map_size = 0 if arg._is_direct else arg.map.values_with_halo.nbytes
                        tot_dat_size = (dat_size + map_size)/1000
                        footprint += tot_dat_size
                    tot_footprint += footprint
                    f.write(template % (loop.it_space.name, str(footprint), str(flops)))
                    tot_flops += flops
                f.write('** Summary: %d KBytes moved, %d Megaflops performed\n' %
                        (tot_footprint, tot_flops))
                probSeed = 0 if MPI.COMM_WORLD.size > 1 else len(loop_chain) / 2
                probNtiles = loop_chain[probSeed].it_space.exec_size / tile_size or 1
                f.write('** KB/tile: %d' % (tot_footprint/probNtiles))
                f.write('  (Estimated: %d tiles)\n' % probNtiles)
                f.write('-' * 68 + '\n')

                # Estimate data reuse
                template = '| %40s | %5s | %-70s |\n'
                f.write('*** Data reuse ***\n')
                f.write(template % ('field', 'type', 'loops'))
                f.write('-' * 125 + '\n')
                reuse = OrderedDict()
                for i, loop in enumerate(loop_chain):
                    for arg in loop.args:
                        values = reuse.setdefault(arg.data, [])
                        if i not in values:
                            values.append(i)
                        if arg._is_indirect:
                            values = reuse.setdefault(arg.map, [])
                            if i not in values:
                                values.append(i)
                for field, positions in reuse.items():
                    reused_in = ', '.join('%d' % j for j in positions)
                    field_type = 'map' if isinstance(field, Map) else 'data'
                    f.write(template % (field.name, field_type, reused_in))
                ideal_reuse = 0
                for field, positions in reuse.items():
                    size = field.values_with_halo.nbytes if isinstance(field, Map) \
                        else field.nbytes
                    # First position needs be cut away as it's the first touch
                    ideal_reuse += (size/1000)*len(positions[1:])
                out = '** Ideal reuse (i.e., no tile growth): %d / %d KBytes (%f %%)\n' % \
                    (ideal_reuse, tot_footprint, float(ideal_reuse)*100/tot_footprint)
                f.write(out)
                f.write('-' * 125 + '\n')
                s.write(out)

        # Finally, get the Executor representation, to be used at executor
        # code generation time
        executor = slope.Executor(inspector)

        kernel = Kernel(tuple(loop.kernel for loop in loop_chain))
        self._schedule = TilingSchedule(self._name, self._schedule, kernel, inspection,
                                        executor, **self._options)

    @property
    def mode(self):
        return self._mode

    @property
    def schedule(self):
        return self._schedule


reads = lambda l: set(a for a in l.args if a.access in [READ, RW])
writes = lambda l: set(a for a in l.args if a.access in [RW, WRITE, MIN, MAX])
incs = lambda l: set(a for a in l.args if a.access in [INC])


def loops_analyzer(loop1, loop2):

    """
    Determine the data dependencies between ``loop1`` and ``loop2``.
    In the sequence of lazily evaluated loops, ``loop1`` comes before ``loop2``.
    Note that INC is treated as a special case of WRITE.

    Return a dictionary of booleans values with the following keys: ::

        * 'homogeneous': True if the loops have same iteration space.
        * 'heterogeneous': True if the loops have different iteration space.
        * 'direct_raw': True if a direct read-after-write dependency is present.
        * 'direct_war': True if a direct write-after-read dependency is present.
        * 'direct_waw': True if a direct write-after-write dependency is present.
        * 'direct_w': OR('direct_raw', 'direct_war', 'direct_waw').
        * 'indirect_raw': True if an indirect (i.e., through maps) read-after-write
            dependency is present.
        * 'indirect_war': True if an indirect write-after-read dependency is present.
        * 'indirect_waw': True if an indirect write-after-write dependency is present.
        * 'indirect_w': OR('indirect_raw', 'indirect_war', 'indirect_waw').
        * 'pure_iai': True if an indirect incr-after-incr dependency is present AND
            no other types of dependencies are present.
    """

    all_reads = lambda l: set(a.data for a in reads(l))
    all_writes = lambda l: set(a.data for a in writes(l))
    all_incs = lambda l: set(a.data for a in incs(l))
    all_inc_writes = lambda l: set(a.data for a in incs(l) | writes(l))

    dir_reads = lambda l: set(a.data for a in reads(l) if a._is_direct)
    dir_writes = lambda l: set(a.data for a in writes(l) if a._is_direct)
    dir_incs = lambda l: set(a.data for a in incs(l) if a._is_direct)
    dir_inc_writes = lambda l: set(a.data for a in incs(l) | writes(l) if a._is_direct)

    ind_reads = lambda l: set(a.data for a in reads(l) if a._is_indirect)
    ind_writes = lambda l: set(a.data for a in writes(l) if a._is_indirect)
    ind_incs = lambda l: set(a.data for a in incs(l) if a._is_indirect)
    ind_inc_writes = lambda l: set(a.data for a in incs(l) | writes(l) if a._is_indirect)

    info = {}

    homogeneous = loop1.it_space == loop2.it_space
    heterogeneous = not homogeneous

    info['homogeneous'] = homogeneous
    info['heterogeneous'] = heterogeneous

    info['direct_raw'] = homogeneous and dir_inc_writes(loop1) & dir_reads(loop2) != set()
    info['direct_war'] = homogeneous and dir_reads(loop1) & dir_inc_writes(loop2) != set()
    info['direct_waw'] = homogeneous and dir_inc_writes(loop1) & dir_inc_writes(loop2) != set()
    info['direct_w'] = info['direct_raw'] or info['direct_war'] or info['direct_waw']

    info['indirect_raw'] = \
        (homogeneous and ind_inc_writes(loop1) & ind_reads(loop2) != set()) or \
        (heterogeneous and all_writes(loop1) & all_reads(loop2) != set())
    info['indirect_war'] = \
        (homogeneous and ind_reads(loop1) & ind_inc_writes(loop2) != set()) or \
        (heterogeneous and all_reads(loop1) & all_writes(loop2) != set())
    info['indirect_waw'] = \
        (homogeneous and ind_inc_writes(loop1) & ind_inc_writes(loop2) != set()) or \
        (heterogeneous and all_writes(loop1) & all_writes(loop2) != set())
    info['indirect_w'] = info['indirect_raw'] or info['indirect_war'] or info['indirect_waw']

    info['pure_iai'] = \
        all_incs(loop1) & all_incs(loop2) != set() and \
        all_writes(loop1) & all_reads(loop2) == set() and \
        all_reads(loop1) & all_writes(loop2) == set() and \
        all_writes(loop1) & all_reads(loop2) == set()

    return info


def build_soft_fusion_kernel(loops, loop_chain_index):
    """
    Build AST and :class:`Kernel` for a sequence of loops suitable to soft fusion.
    """

    kernels = [l.kernel for l in loops]
    asts = [k._original_ast if k._code else k._ast for k in kernels]
    base_ast, fuse_asts = dcopy(asts[0]), asts[1:]

    base_fundecl = FindInstances(ast.FunDecl).visit(base_ast)[ast.FunDecl][0]
    for unique_id, _fuse_ast in enumerate(fuse_asts, 1):
        fuse_ast = dcopy(_fuse_ast)
        fuse_fundecl = FindInstances(ast.FunDecl).visit(fuse_ast)[ast.FunDecl][0]
        # 1) Extend function name
        base_fundecl.name = "%s_%s" % (base_fundecl.name, fuse_fundecl.name)
        # 2) Concatenate the arguments in the signature
        base_fundecl.args.extend(fuse_fundecl.args)
        # 3) Uniquify symbols identifiers
        fuse_symbols = SymbolReferences().visit(fuse_ast)
        for decl in fuse_fundecl.args:
            for symbol, _ in fuse_symbols[decl.sym.symbol]:
                symbol.symbol = "%s_%d" % (symbol.symbol, unique_id)
        # 4) Concatenate bodies
        base_fundecl.body.extend([ast.FlatBlock("\n\n// Fused kernel: \n\n")] +
                                 fuse_fundecl.body)

    # Eliminate redundancies in the /fused/ kernel signature
    Filter().kernel_args(loops, base_fundecl)

    return Kernel(kernels, base_ast, loop_chain_index)


def build_hard_fusion_kernel(base_loop, fuse_loop, fusion_map, loop_chain_index):
    """
    Build AST and :class:`Kernel` for two loops suitable to hard fusion.

    The AST consists of three functions: fusion, base, fuse. base and fuse
    are respectively the ``base_loop`` and the ``fuse_loop`` kernels, whereas
    fusion is the orchestrator that invokes, for each ``base_loop`` iteration,
    base and, if still to be executed, fuse.

    The orchestrator has the following structure: ::

        fusion (buffer, ..., executed):
            base (buffer, ...)
            for i = 0 to arity:
                if not executed[i]:
                    additional pointer staging required by kernel2
                    fuse (sub_buffer, ...)
                    insertion into buffer

    The executed array tracks whether the i-th iteration (out of /arity/)
    adjacent to the main kernel1 iteration has been executed.
    """

    finder = FindInstances((ast.FunDecl, ast.PreprocessNode))

    # Hard fusion occurs on fresh copies of the /base/ and /fuse/ ASTs as
    # the optimization process in COFFEE is different if kernels get fused.

    base = base_loop.kernel
    base_ast = dcopy(base._original_ast) if base._code else dcopy(base._ast)
    base_info = finder.visit(base_ast)
    base_headers = base_info[ast.PreprocessNode]
    base_fundecl = base_info[ast.FunDecl]
    assert len(base_fundecl) == 1
    base_fundecl = base_fundecl[0]

    fuse = fuse_loop.kernel
    fuse_ast = dcopy(fuse._original_ast) if fuse._code else dcopy(fuse._ast)
    fuse_info = finder.visit(fuse_ast)
    fuse_headers = fuse_info[ast.PreprocessNode]
    fuse_fundecl = fuse_info[ast.FunDecl]
    assert len(fuse_fundecl) == 1
    fuse_fundecl = fuse_fundecl[0]

    # Create /fusion/ arguments and signature
    body = ast.Block([])
    fusion_name = '%s_%s' % (base_fundecl.name, fuse_fundecl.name)
    fusion_args = dcopy(base_fundecl.args + fuse_fundecl.args)
    fusion_fundecl = ast.FunDecl(base_fundecl.ret, fusion_name, fusion_args, body)

    # Make sure kernel names are unique
    base_fundecl.name = "%s_base" % base_fundecl.name
    fuse_fundecl.name = "%s_fuse" % fuse_fundecl.name

    # Filter out duplicate arguments, and append extra arguments to the fundecl
    binding = WeakFilter().kernel_args([base_loop, fuse_loop], fusion_fundecl)
    fusion_fundecl.args += [ast.Decl('int*', 'executed'),
                            ast.Decl('int*', 'fused_iters'),
                            ast.Decl('int', 'i')]

    # Which args are actually used in /fuse/, but not in /base/ ?
    # The gather for such arguments is moved to /fusion/, to avoid any
    # usless LOAD from memory
    base_symbols = SymbolReferences().visit(base_fundecl.body)
    fuse_symbols = SymbolReferences().visit(fuse_fundecl.body)
    base_funcall_syms, unshared = [], OrderedDict()
    for arg, decl in binding.items():
        if decl.sym.symbol in set(fuse_symbols) - set(base_symbols):
            base_funcall_sym = ast.Symbol('NULL')
            unshared.setdefault(decl, arg)
        else:
            base_funcall_sym = ast.Symbol(decl.sym.symbol)
        if arg in base_loop.args:
            base_funcall_syms.append(base_funcall_sym)
    for decl, arg in unshared.items():
        decl.typ = 'double*'
        decl.sym.symbol = arg.c_arg_name()
        fusion_fundecl.args.insert(fusion_fundecl.args.index(decl) + 1,
                                   ast.Decl('int*', arg.c_map_name(0, 0)))

    # Append the invocation of /base/; then, proceed with the invocation
    # of the /fuse/ kernels
    body.children.append(ast.FunCall(base_fundecl.name, *base_funcall_syms))

    for idx in range(fusion_map.arity):

        fused_iter = 'fused_iters[%d]' % idx
        fuse_funcall = ast.FunCall(fuse_fundecl.name)
        if_cond = ast.Not(ast.Symbol('executed', (fused_iter,)))
        if_update = ast.Assign(ast.Symbol('executed', (fused_iter,)), 1)
        if_body = ast.Block([fuse_funcall, if_update], open_scope=True)
        if_exec = ast.If(if_cond, [if_body])
        body.children.extend([ast.FlatBlock('\n'), if_exec])

        # Modify the /fuse/ kernel
        # This is to take into account that many arguments are shared with
        # /base/, so they will only staged once for /base/. This requires
        # tweaking the way the arguments are declared and accessed in /fuse/.
        # For example, the shared incremented array (called /buffer/ in
        # the pseudocode in the comment above) now needs to take offsets
        # to be sure the locations that /base/ is supposed to increment are
        # actually accessed. The same concept apply to indirect arguments.
        init = lambda v: '{%s}' % ', '.join([str(j) for j in v])
        for i, fuse_loop_arg in enumerate(fuse_loop.args):
            fuse_kernel_arg = binding[fuse_loop_arg]
            buffer = '%s_vec' % fuse_kernel_arg.sym.symbol

            # How should I use the temporaries ?
            if fuse_loop_arg.access == INC:
                op = ast.Incr
                lvalue, rvalue = fuse_kernel_arg.sym.symbol, buffer
                extend_if_body = lambda body, block: body.children.extend(block)
                buffer_decl = ast.Decl(fuse_kernel_arg.typ, buffer,
                                       qualifiers=fuse_kernel_arg.qual)
            elif fuse_loop_arg.access == READ:
                op = ast.Assign
                lvalue, rvalue = buffer, fuse_kernel_arg.sym.symbol
                extend_if_body = lambda body, block: \
                    [body.children.insert(0, b) for b in reversed(block)]
                pointers = fuse_kernel_arg.typ.count('*') + len(fuse_kernel_arg.pointers)
                buffer_decl = ast.Decl(fuse_kernel_arg.typ, buffer,
                                       qualifiers=fuse_kernel_arg.qual,
                                       pointers=['' for j in range(pointers-1)])

            # Now handle arguments depending on their type ...
            if fuse_loop_arg._is_mat:
                # ... Handle Mats
                staging = []
                for b in fused_inc_arg._block_shape:
                    for rc in b:
                        lvalue = ast.Symbol(lvalue, (idx, idx),
                                            ((rc[0], 'j'), (rc[1], 'k')))
                        rvalue = ast.Symbol(rvalue, ('j', 'k'))
                        staging = ItSpace(mode=0).to_for([(0, rc[0]), (0, rc[1])],
                                                         ('j', 'k'),
                                                         [op(lvalue, rvalue)])[:1]
                # Set up the temporary
                buffer_decl.sym.rank = fuse_kernel_arg.sym.rank
                if fuse_loop_arg.access == INC:
                    buffer_decl.init = ast.ArrayInit(init([init([0.0])]))

            elif fuse_loop_arg._is_indirect:
                # ... Handle indirect arguments. At the C level, these arguments
                # are of pointer type, so simple pointer arithmetic is used
                # to ensure the kernel accesses are to the correct locations
                fuse_arity = fuse_loop_arg.map.arity
                base_arity = fuse_arity*fusion_map.arity
                cdim = fuse_loop_arg.data.dataset.cdim
                size = fuse_arity*cdim
                # Set the proper storage layout before invoking /fuse/
                ofs_vals = [[base_arity*j + k for k in range(fuse_arity)]
                            for j in range(cdim)]
                ofs_vals = [[fuse_arity*j + k for k in flatten(ofs_vals)]
                            for j in range(fusion_map.arity)]
                ofs_vals = list(flatten(ofs_vals))
                indices = [ofs_vals[idx*size + j] for j in range(size)]
                # Set up the temporary and stage (gather) data into it
                buffer_decl.sym.rank = (size,)
                if fuse_loop_arg.access == INC:
                    buffer_decl.init = ast.ArrayInit(init([0.0]))
                    staging = [op(ast.Symbol(lvalue, (k,)), ast.Symbol(rvalue, (j,)))
                               for j, k in enumerate(indices)]
                elif fuse_kernel_arg in unshared:
                    staging = unshared[fuse_kernel_arg].c_vec_init(False).split('\n')
                    staging = [j for i, j in enumerate(staging) if i in indices]
                    rvalues = [ast.FlatBlock(i.split('=')[1]) for i in staging]
                    lvalues = [ast.Symbol(buffer, (i,)) for i in range(len(staging))]
                    staging = [ast.Assign(i, j) for i, j in zip(lvalues, rvalues)]
                else:
                    staging = [op(ast.Symbol(lvalue, (j,)), ast.Symbol(rvalue, (k,)))
                               for j, k in enumerate(indices)]

            else:
                # Nothing special to do for direct arguments
                continue

            # Update the If-then AST body
            extend_if_body(if_exec.children[0], staging)
            if_exec.children[0].children.insert(0, buffer_decl)
            fuse_funcall.children.append(ast.Symbol(buffer))

    fused_headers = set([str(h) for h in base_headers + fuse_headers])
    fused_ast = ast.Root([ast.PreprocessNode(h) for h in fused_headers] +
                         [base_fundecl, fuse_fundecl, fusion_fundecl])

    # Track position of Args that need a postponed gather
    # Can't track Args themselves as they change across different parloops
    fargs = {fusion_args.index(i): ('postponed', False) for i in unshared.keys()}
    fargs.update({len(set(binding.values())): ('onlymap', True)})

    return Kernel([base, fuse], fused_ast, loop_chain_index), fargs


def create_slope_set(op2set, extra_halo, insp_sets=None):
    """
    Convert an OP2 set to a set suitable for the SLOPE Python interface.
    Also check that the halo region us sufficiently depth for tiling.
    """
    SlopeSet = namedtuple('SlopeSet', 'name core boundary nonexec superset')

    partitioning = op2set._partitioning if hasattr(op2set, '_partitioning') else None
    if not isinstance(op2set, Subset):
        name = op2set.name
        superset = None
    else:
        name = "%s_ss" % op2set
        superset = s.superset.name

    if slope.get_exec_mode() not in ['OMP_MPI', 'ONLY_MPI']:
        core_size = op2set.core_size
        boundary_size = op2set.exec_size - op2set.core_size
        nonexec_size = op2set.total_size - op2set.exec_size
    else:
        # Assume [1, ..., N] levels of halo regions
        # Each level is represented by (core, owned, exec, nonexec)
        level_N = op2set._deep_size[-1]
        core_size = level_N[0]
        boundary_size = level_N[2] - core_size
        nonexec_size = level_N[3] - level_N[2]
        if extra_halo and nonexec_size == 0:
            level_E = op2set._deep_size[-2]
            boundary_size = level_E[2] - core_size
            nonexec_size = level_E[3] - level_E[2]

    slope_set = SlopeSet(name, core_size, boundary_size, nonexec_size, superset)
    insp_sets[slope_set] = partitioning

    return slope_set
