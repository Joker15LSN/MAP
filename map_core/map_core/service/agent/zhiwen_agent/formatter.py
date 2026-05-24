import json
from typing import Optional, Union

from .enterprise_kb_api import RetrieveItemSchema, RetrieveResponseDataSchema

DOC_TEMPLATE = "--DOC start--\n{}\n--DOC end--"
ITEM_TEMPLATE = "title: {title}, from source: {source}, create_time: {create_time}\n chunks: {chunks}"

def format_search_item(item_data: Union[dict, RetrieveItemSchema]) -> Optional[str]:
    if isinstance(item_data, RetrieveItemSchema):
        title = item_data.title
        contents = item_data.contents
        source = item_data.source
        create_time = item_data.create_time if item_data.create_time else 'unknown'
        create_time = create_time[:10] #仅保留yyyy-mm-dd
        if contents:
            return ITEM_TEMPLATE.format(
                title=title,
                source=source,
                chunks='...'.join(contents),
                create_time=create_time,
            )
    else:
        # dict type
        title = item_data.get("title")
        contents = item_data.get("contents")
        source = item_data.get("source")
        create_time = item_data.get("create_time") if item_data.get("create_time")  else 'unknown'
        create_time = create_time[:10] # type: ignore #仅保留yyyy-mm-dd 
        if contents:
            return ITEM_TEMPLATE.format(
                title=title,
                source=source,
                chunks='...'.join(contents),
                create_time=create_time,
            )
    return None

def format_retrieve_response(res_data: Union[dict, RetrieveResponseDataSchema]) -> str:
    ...
    # summary = json.dumps(res_data, ensure_ascii=False) if res_data else "无检索结果。"
    if isinstance(res_data, RetrieveResponseDataSchema):
        if res_data.items:
            res_parts = []
            for item in res_data.items:
                formatted_item = format_search_item(item_data=item)
                if formatted_item:
                    res_parts.append(DOC_TEMPLATE.format(formatted_item))
            return '\n'.join(res_parts)
        else:
            return "无检索结果。"
    else:
        # dict type
        items = res_data.get("items", [])
        if items:
            res_parts = []
            for item in items:
                formatted_item = format_search_item(item_data=item)
                if formatted_item:
                    res_parts.append(DOC_TEMPLATE.format(formatted_item))
            return '\n'.join(res_parts)
        else:
            return "无检索结果。"

"""
formatted_text = f"--DOC start--\n{formatted_text}\n--DOC end--"

"""