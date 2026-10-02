from pydantic import BaseModel, Field
from typing import Optional, Literal

class CartIntentModel(BaseModel):
    intent: Literal[
        "select_variant",
        "add_to_cart",
        "remove_from_cart",
        "checkout",
        "confirm_yes",
        "confirm_no",
        "filter_products",
        "general_question"
    ] = Field(description="The user's intent")
    product_ref: Optional[str] = Field(default=None, description="Product handle (the [bracketed] identifier) or null")
    variant_ref: Optional[str] = Field(default=None, description="Size/color/sku or null")
    quantity: int = Field(default=1, description="Quantity of items")
    go_to_checkout: bool = Field(default=False, description="True if customer wants to go to checkout")
    answer: str = Field(default="I didn't understand that. Could you please clarify?", description="Short conversational response")

class FiltersModel(BaseModel):
    product_name: Optional[str] = None
    product_type: Optional[str] = None
    color: Optional[str] = None
    size: Optional[str] = None
    vendor: Optional[str] = None
    max_price: Optional[float] = None
    min_price: Optional[float] = None

class BrowseIntentModel(BaseModel):
    intent: Literal[
        "search_products_in_collection",
        "list_collections",
        "clear_collection_filter",
        "filter_products",
        "general_question"
    ] = Field(default="general_question")
    collection_name: Optional[str] = None
    filters: FiltersModel = Field(default_factory=FiltersModel)
    answer: str = Field(default="I didn't understand that. Please try again.")
