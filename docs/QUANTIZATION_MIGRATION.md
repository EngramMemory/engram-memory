# Scalar Quantization Migration Guide

Engram v2024.3 introduces scalar quantization for 4x memory reduction with no recall loss. This guide helps you migrate existing collections to use quantization.

## What's New

- **Memory Reduction**: ~4x less RAM usage for vector storage
- **No Quality Loss**: 99th percentile quantization preserves search accuracy  
- **Faster at Scale**: More memories fit in RAM = faster search performance
- **Automatic**: New installations get quantization by default

## For New Installations

Quantization is automatically enabled when you run:

```bash
cd engram-memory
bash scripts/setup.sh
```

New collections will be created with scalar quantization configuration.

## For Existing Collections

If you already have an `agent-memory` collection, you have two options:

### Option 1: Enable Quantization on Existing Collection

```bash
# Enable quantization on existing collection (requires Qdrant v1.8+)
curl -X PATCH http://localhost:6333/collections/agent-memory \
  -H "Content-Type: application/json" \
  -d '{
    "quantization_config": {
      "scalar": {
        "type": "int8",
        "quantile": 0.99,
        "always_ram": true
      }
    }
  }'
```

**Note**: This will take time to process depending on collection size. Monitor with:

```bash
curl http://localhost:6333/collections/agent-memory
```

Look for `"status": "green"` to confirm completion.

### Option 2: Backup, Recreate, and Restore (Recommended)

This ensures optimal performance and guarantees the quantization is applied correctly:

```bash
# 1. Export existing memories
curl "http://localhost:6333/collections/agent-memory/points/scroll" \
  -H "Content-Type: application/json" \
  -d '{"limit": 100000, "with_vector": true, "with_payload": true}' \
  > memory_backup.json

# 2. Delete existing collection
curl -X DELETE http://localhost:6333/collections/agent-memory

# 3. Recreate with quantization (run setup script)
cd engram-memory
bash scripts/setup.sh

# 4. Restore memories
curl -X PUT http://localhost:6333/collections/agent-memory/points \
  -H "Content-Type: application/json" \
  -d @memory_backup.json

# 5. Verify collection has quantization
curl http://localhost:6333/collections/agent-memory | grep -A5 quantization_config
```

## Verification

Check that quantization is active:

```bash
# Check collection configuration
curl http://localhost:6333/collections/agent-memory

# Should show:
# "quantization_config": {
#   "scalar": {
#     "type": "int8",
#     "quantile": 0.99,
#     "always_ram": true
#   }
# }
```

Verify memory usage reduction:

```bash
# Check collection stats
curl http://localhost:6333/collections/agent-memory

# Look for:
# "vectors_count": <number>
# "indexed_vectors_count": <number>
# "points_count": <number>

# Memory usage should be ~4x lower compared to before
```

## Performance Benefits

**Before Quantization:**
- 768-dim vector = 768 × 4 bytes = 3,072 bytes per vector
- 10,000 memories = ~30 MB of vector data

**After Quantization:**
- 768-dim vector = 768 × 1 byte = 768 bytes per vector  
- 10,000 memories = ~7.5 MB of vector data
- **Result**: 4x memory reduction + same search speed

## Compatibility

- **Qdrant Version**: Requires v1.8.0+ (setup script uses v1.11.3)
- **Existing Code**: No changes needed to Engram plugin
- **Search Quality**: Identical results (99th percentile preserves accuracy)
- **Mixed Collections**: Quantized and non-quantized vectors can coexist

## Troubleshooting

**Collection won't enable quantization:**
- Check Qdrant version: `curl http://localhost:6333/health`
- Ensure collection has vectors before enabling quantization
- Try the backup/recreate approach instead

**Search quality seems degraded:**
- Verify quantile is set to 0.99 (not 0.9 or lower)
- Check that `always_ram: true` is set
- Compare with backup of original collection

**Memory usage didn't decrease:**
- Wait for reindexing to complete (check collection status)
- Restart Qdrant container to clear old cached data
- Verify quantization is actually enabled in collection config

## Support

If you encounter issues with quantization:

1. Check Qdrant logs: `docker logs qdrant-memory`
2. Verify collection status: `curl http://localhost:6333/collections/agent-memory`
3. Test with a small collection first before migrating large datasets

## Rollback

If needed, you can disable quantization:

```bash
# Remove quantization (reverts to full precision)
curl -X PATCH http://localhost:6333/collections/agent-memory \
  -H "Content-Type: application/json" \
  -d '{"quantization_config": null}'
```

**Note**: This will increase memory usage back to full precision levels.