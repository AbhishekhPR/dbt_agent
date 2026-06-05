import json

with open('test_project/target/manifest.json') as f:
    m = json.load(f)

nodes = m.get('nodes', {})
sources = m.get('sources', {})

print(f"Total nodes: {len(nodes)}")
print(f"Total sources: {len(sources)}")
print()

for nid, n in nodes.items():
    if n.get('resource_type') == 'model':
        deps = n.get('depends_on', {}).get('nodes', [])
        name = n.get('name')
        print(f"Model: {name}")
        print(f"  depends_on nodes: {deps}")
        print()

print("--- SOURCES ---")
for sid, s in sources.items():
    print(f"Source: {s.get('name')} | identifier: {s.get('identifier')}")
