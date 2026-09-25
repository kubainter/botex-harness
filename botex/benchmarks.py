# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
import urllib.request
import json
from typing import List, Dict, Any

def get_recommended_models(task_type: str = "coding", limit: int = 5) -> List[Dict[str, Any]]:
    """
    Fetches model recommendations. Uses OpenRouter's /api/v1/models endpoint to
    find the most cost-effective models suitable for the given task.
    """
    url = "https://openrouter.ai/api/v1/models"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return [{"error": f"Failed to fetch models from OpenRouter: {e}"}]

    models = data.get("data", [])
    
    scored_models = []
    for m in models:
        slug = m.get("id", "")
        pricing = m.get("pricing", {})
        context_length = m.get("context_length", 0)
        
        try:
            pin = float(pricing.get("prompt", 0)) * 1_000_000
            pout = float(pricing.get("completion", 0)) * 1_000_000
        except (TypeError, ValueError):
            continue
            
        if pin < 0 or pout < 0:
            continue
            
        cost_score = pin + pout
        
        # ZDR (Zero Data Retention) enforcement:
        # OpenRouter's free fallback providers (like Google AI Studio) may collect data.
        # To strictly enforce ZDR, we exclude any model with ':free' in the slug unless overridden.
        if ":free" in slug.lower():
            continue

        # Simple heuristic for 'coding' task:
        # We want high context length, and reasonable price.
        # Let's filter out very expensive ones and sort by a heuristic score.
        if task_type == "coding":
            if context_length < 32000:
                continue
            # Prefer models known for coding (claude, gpt, qwen, llama)
            if "coder" not in slug.lower() and "claude-3.5-sonnet" not in slug.lower() and "gpt-4o" not in slug.lower():
                # apply a slight penalty to general models if not explicitly known for coding
                cost_score *= 1.5

        scored_models.append({
            "id": slug,
            "name": m.get("name", slug),
            "context_length": context_length,
            "pricing": {
                "prompt_1M": round(pin, 4),
                "completion_1M": round(pout, 4)
            },
            "_score": cost_score
        })
        
    # Sort by lowest cost score
    scored_models.sort(key=lambda x: x["_score"])
    
    # Remove internal sorting score
    for m in scored_models:
        del m["_score"]
        
    return scored_models[:limit]
