"""Sphinx configuration for the agent-tracker documentation."""

project = "agent-tracker"
copyright = "2026, agent-tracker contributors"
author = "agent-tracker contributors"

extensions = ["myst_parser"]

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}
master_doc = "index"
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]
suppress_warnings = ["myst.xref_missing"]

html_theme = "furo"
html_title = "agent-tracker"

myst_heading_anchors = 3
myst_enable_extensions = ["colon_fence"]
