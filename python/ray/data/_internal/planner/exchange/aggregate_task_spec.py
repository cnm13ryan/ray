from typing import List, Optional, Tuple, Union

from ray.data._internal.aggregate import Count, _AggregateOnKeyBase
from ray.data._internal.planner.exchange.interfaces import ExchangeTaskSpec
from ray.data._internal.planner.exchange.sort_task_spec import SortKey
from ray.data._internal.table_block import TableBlockAccessor
from ray.data.aggregate import AggregateFn
from ray.data.block import Block, BlockAccessor, BlockExecStats, BlockMetadata, KeyType


class SortAggregateTaskSpec(ExchangeTaskSpec):
    """
    The implementation for sort-based aggregate tasks.

    Aggregate is done in 2 steps: partial aggregate of individual blocks, and
    final aggregate of sorted blocks.

    Partial aggregate (`map`): each block is sorted locally, then partitioned into
    smaller blocks according to the boundaries. Each partitioned block is aggregated
    separately, then passed to a final aggregate task.

    Final aggregate (`reduce`): each task would receive a block from every worker that
    consists of items in a certain range. It then merges the sorted blocks and
    aggregates on-the-fly.
    """

    def __init__(
        self,
        boundaries: List[KeyType],
        key: Optional[str],
        aggs: List[AggregateFn],
        batch_format: str,
    ):
        super().__init__(
            map_args=[boundaries, key, aggs],
            reduce_args=[key, aggs, batch_format],
        )

    @staticmethod
    def map(
        idx: int,
        block: Block,
        output_num_blocks: int,
        boundaries: List[KeyType],
        key: Union[str, List[str], None],
        aggs: List[AggregateFn],
    ) -> List[Union[BlockMetadata, Block]]:
        stats = BlockExecStats.builder()

        block = SortAggregateTaskSpec._prune_unused_columns(block, key, aggs)
        if key is None:
            partitions = [block]
        else:
            partitions = BlockAccessor.for_block(block).sort_and_partition(
                boundaries,
                SortKey(key),
            )
        parts = [BlockAccessor.for_block(p).combine(key, aggs) for p in partitions]
        meta = BlockAccessor.for_block(block).get_metadata(exec_stats=stats.build())
        return parts + [meta]

    @staticmethod
    def reduce(
        key: Optional[str],
        aggs: List[AggregateFn],
        batch_format: str,
        *mapper_outputs: List[Block],
        partial_reduce: bool = False,
    ) -> Tuple[Block, BlockMetadata]:
        normalized_blocks = TableBlockAccessor.normalize_block_types(
            mapper_outputs, normalize_type=batch_format
        )
        return BlockAccessor.for_block(normalized_blocks[0]).aggregate_combined_blocks(
            list(normalized_blocks), key, aggs, finalize=not partial_reduce
        )

    @staticmethod
    def _prune_unused_columns(
        block: Block,
        key: Union[str, List[str]],
        aggs: Tuple[AggregateFn],
    ) -> Block:
        """Prune unused columns from block before aggregate."""
        prune_columns = True
        columns = set()

        if isinstance(key, str):
            columns.add(key)
        elif isinstance(key, list):
            columns.update(key)
        elif callable(key):
            prune_columns = False

        for agg in aggs:
            if isinstance(agg, _AggregateOnKeyBase) and isinstance(agg._key_fn, str):
                columns.add(agg._key_fn)
            elif not isinstance(agg, Count):
                # Don't prune columns if any aggregate key is not string.
                prune_columns = False

        block_accessor = BlockAccessor.for_block(block)
        if (
            prune_columns
            and isinstance(block_accessor, TableBlockAccessor)
            and block_accessor.num_rows() > 0
        ):
            return block_accessor.select(list(columns))
        else:
            return block
