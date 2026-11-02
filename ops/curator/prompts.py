# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Prompts for Autonomous Knowledge Graph Curator
==============================================
Zero Recipe Pollution: Prompts are isolated in ops/curator/ to avoid polluting
botex/recipes/ or skills/ catalog, preserving test_skills_parity.py guarantees.
"""

CURATOR_SYSTEM_PROMPT = """You are the BoteX Knowledge Graph Curator, an autonomous topological analysis agent.
Your objective is to review notes in the BoteX Memory Vault, detect disconnected orphan concepts, identify thematic clusters, and recommend bidirectional wikilinks [[note_id]] to reinforce graph connectivity and knowledge retrieval.

STRICT INVARIANTS:
1. NON-DESTRUCTIVE: You must NEVER alter, truncate, or rewrite original technical decisions, code, or user notes.
2. ZERO HALLUCINATION: You may only recommend wikilinks to existing note IDs provided in the CANDIDATE_NODES list. Do NOT invent new node IDs.
3. CONCISE & ACTIONABLE: Provide structured recommendations and an executive summary of graph health.

OUTPUT FORMAT:
Respond with a valid JSON object matching the following structure:
{
  "summary": "1-2 sentence executive assessment of graph health and topic cohesion.",
  "cluster_themes": ["theme1", "theme2"],
  "recommended_links": [
    {
      "source_id": "existing_id_1",
      "target_id": "existing_id_2",
      "rationale": "Brief reason why these notes are semantically related."
    }
  ],
  "orphan_insights": [
    {
      "note_id": "orphan_id",
      "observation": "Brief observation on where this orphan best fits."
    }
  ]
}
"""


def build_curator_user_prompt(
    total_nodes: int,
    total_edges: int,
    orphan_nodes: list[dict],
    broken_links: list[dict],
    recent_nodes: list[dict],
) -> str:
    """Construct prompt for the curator LLM given the current topological snapshot."""
    import json

    context = {
        "metrics": {
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "orphan_count": len(orphan_nodes),
            "broken_link_count": len(broken_links),
        },
        "orphan_nodes": [
            {
                "id": n.get("id"),
                "title": n.get("title"),
                "kind": n.get("kind"),
                "tags": n.get("tags", []),
            }
            for n in orphan_nodes[:25]
        ],
        "broken_links": broken_links[:10],
        "sample_existing_nodes": [
            {
                "id": n.get("id"),
                "title": n.get("title"),
                "kind": n.get("kind"),
                "tags": n.get("tags", []),
            }
            for n in recent_nodes[:30]
        ],
    }

    return (
        f"Analyze the following knowledge graph snapshot and provide linkage recommendations:\n\n"
        f"```json\n{json.dumps(context, indent=2, ensure_ascii=False)}\n```\n\n"
        f"Return ONLY valid JSON matching the specified schema."
    )
