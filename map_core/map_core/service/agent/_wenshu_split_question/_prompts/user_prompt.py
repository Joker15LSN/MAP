IDENTIFY_METRICS_USER_PROMPT = """Given the user's question: "{question}"
{dimension_context}

And the available metrics:
{metrics_list}

{split_question_examples}
"""

GENERATE_SUB_QUESTIONS_USER_PROMPT = """Original question: "{question}"
Target metric: {metric_name} ({metric_code})
Metric meaning: {metric_meaning}
Dimension list: {dimension_list}
{dimension_context}

--- Dimension handling instructions ---
{dimension_instruction}

{split_question_examples}
"""
