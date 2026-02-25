from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in self._tools.values()
        ]

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name not in self._tools:
            raise ValueError(f"Unknown tool: {tool_name}")
        tool = self._tools[tool_name]
        self._validate_input_schema(tool.input_schema, arguments)
        return tool.handler(arguments)

    def _validate_input_schema(self, schema: dict[str, Any], value: Any, *, path: str = "arguments") -> None:
        if not isinstance(schema, dict):
            return

        expected_type = schema.get("type")

        if expected_type == "object":
            if not isinstance(value, dict):
                raise ValueError(f"{path} must be an object")

            properties = schema.get("properties", {})
            if not isinstance(properties, dict):
                properties = {}

            required = schema.get("required", [])
            if not isinstance(required, list):
                required = []

            for key in required:
                if isinstance(key, str) and key not in value:
                    raise ValueError(f"Missing required field: {path}.{key}")

            additional_properties = schema.get("additionalProperties", True)
            if additional_properties is False:
                for key in value.keys():
                    if key not in properties:
                        raise ValueError(f"Unexpected field: {path}.{key}")

            for key, property_schema in properties.items():
                if key in value:
                    self._validate_input_schema(property_schema, value[key], path=f"{path}.{key}")
            return

        if expected_type == "array":
            if not isinstance(value, list):
                raise ValueError(f"{path} must be an array")
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    self._validate_input_schema(item_schema, item, path=f"{path}[{index}]")
            return

        if expected_type == "string":
            if not isinstance(value, str):
                raise ValueError(f"{path} must be a string")
            return

        if expected_type == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"{path} must be a boolean")
            return

        if expected_type == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{path} must be an integer")
            return
