"""Task and model presets for the dashboard UI.

These provide quick-start templates that users can select and customize.
Task presets include common automation scenarios.
Model presets bundle recommended model combinations for different hardware.
"""

TASK_PRESETS = [
    {
        "id": "google_search",
        "name": "Google search",
        "icon": "search",
        "category": "research",
        "task": "Search Google for '{query}' and summarize the top 5 results",
        "start_url": "https://www.google.com",
        "variables": [
            {"name": "query", "label": "Search query", "placeholder": "best open source LLMs 2025"}
        ],
    },
    {
        "id": "flight_search",
        "name": "Flight search",
        "icon": "plane",
        "category": "shopping",
        "task": "Go to Google Flights and find the cheapest round-trip flight from {origin} to {destination} departing {depart_date} and returning {return_date}. Report the top 3 cheapest options.",
        "start_url": "https://www.google.com/travel/flights",
        "variables": [
            {"name": "origin", "label": "From", "placeholder": "LAX"},
            {"name": "destination", "label": "To", "placeholder": "JFK"},
            {"name": "depart_date", "label": "Departure date", "placeholder": "Dec 15"},
            {"name": "return_date", "label": "Return date", "placeholder": "Dec 22"},
        ],
    },
    {
        "id": "product_compare",
        "name": "Product comparison",
        "icon": "scale",
        "category": "shopping",
        "task": "Search Amazon for '{product}' and compare the top 3 results by price, rating, and reviews. Create a summary comparison.",
        "start_url": "https://www.amazon.com",
        "variables": [
            {"name": "product", "label": "Product", "placeholder": "wireless noise cancelling headphones"}
        ],
    },
    {
        "id": "job_search",
        "name": "Job search",
        "icon": "briefcase",
        "category": "research",
        "task": "Search LinkedIn Jobs for '{role}' positions in {location}. List the top 5 results with company name, title, and key requirements.",
        "start_url": "https://www.linkedin.com/jobs",
        "variables": [
            {"name": "role", "label": "Role", "placeholder": "ML Engineer"},
            {"name": "location", "label": "Location", "placeholder": "San Francisco"},
        ],
    },
    {
        "id": "wikipedia_research",
        "name": "Wikipedia deep dive",
        "icon": "book",
        "category": "research",
        "task": "Go to Wikipedia and research '{topic}'. Navigate through related articles to build a comprehensive summary covering key facts, history, and related topics.",
        "start_url": "https://en.wikipedia.org",
        "variables": [
            {"name": "topic", "label": "Topic", "placeholder": "quantum computing"}
        ],
    },
    {
        "id": "form_fill",
        "name": "Form filling",
        "icon": "edit",
        "category": "automation",
        "task": "Navigate to {url} and fill in the form with the following information: {details}",
        "start_url": "",
        "variables": [
            {"name": "url", "label": "Form URL", "placeholder": "https://example.com/signup"},
            {"name": "details", "label": "Form details", "placeholder": "Name: John Doe, Email: john@example.com"},
        ],
    },
    {
        "id": "price_monitor",
        "name": "Price check",
        "icon": "tag",
        "category": "shopping",
        "task": "Go to {url} and find the current price of '{item}'. Report the price, availability, and any active discounts.",
        "start_url": "",
        "variables": [
            {"name": "url", "label": "Store URL", "placeholder": "https://www.bestbuy.com"},
            {"name": "item", "label": "Item", "placeholder": "RTX 4090"},
        ],
    },
    {
        "id": "custom",
        "name": "Custom task",
        "icon": "terminal",
        "category": "custom",
        "task": "",
        "start_url": "https://www.google.com",
        "variables": [],
    },
]

MODEL_PRESETS = [
    {
        "id": "full_power",
        "name": "Full power",
        "description": "Best quality. Requires ~48GB+ VRAM.",
        "icon": "zap",
        "planner": {"model": "qwen2.5:72b", "backend": "ollama"},
        "executor": {"model": "qwen2.5:7b", "backend": "ollama"},
        "validator": {"model": "minicpm-v:8b", "backend": "ollama"},
    },
    {
        "id": "balanced",
        "name": "Balanced",
        "description": "Good quality with moderate resources. ~16-24GB VRAM.",
        "icon": "sliders",
        "planner": {"model": "qwen2.5:14b", "backend": "ollama"},
        "executor": {"model": "qwen2.5:7b", "backend": "ollama"},
        "validator": {"model": "minicpm-v:8b", "backend": "ollama"},
    },
    {
        "id": "lightweight",
        "name": "Lightweight",
        "description": "Runs on most GPUs. ~8-12GB VRAM.",
        "icon": "feather",
        "planner": {"model": "qwen2.5:7b", "backend": "ollama"},
        "executor": {"model": "qwen2.5:3b", "backend": "ollama"},
        "validator": {"model": "minicpm-v:8b", "backend": "ollama"},
    },
    {
        "id": "cpu_only",
        "name": "CPU only",
        "description": "No GPU required. Slowest but works anywhere.",
        "icon": "cpu",
        "planner": {"model": "qwen2.5:3b", "backend": "ollama"},
        "executor": {"model": "qwen2.5:1.5b", "backend": "ollama"},
        "validator": {"model": "moondream:1.8b", "backend": "ollama"},
    },
    {
        "id": "llama_stack",
        "name": "Llama stack",
        "description": "All Meta Llama models. ~24GB VRAM.",
        "icon": "layers",
        "planner": {"model": "llama3.1:70b", "backend": "ollama"},
        "executor": {"model": "llama3.2:3b", "backend": "ollama"},
        "validator": {"model": "llama3.2-vision:11b", "backend": "ollama"},
    },
    {
        "id": "grammar_enforced",
        "name": "Grammar enforced",
        "description": "Uses llama.cpp for executor with GBNF grammar. Most reliable actions.",
        "icon": "shield",
        "planner": {"model": "qwen2.5:14b", "backend": "ollama"},
        "executor": {"model": "/path/to/qwen2.5-7b.Q4_K_M.gguf", "backend": "llamacpp"},
        "validator": {"model": "minicpm-v:8b", "backend": "ollama"},
    },
]
