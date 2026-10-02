import os
import sys
import asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from shopify_service import get_products, get_collection_products, get_collections
from main import _handle_browse, _handle_awaiting_variant
from conversation_state import ConversationState

async def run_qa():
    print("Fetching live products...")
    # Assuming .env is loaded via llm_service or we just rely on defaults if they exist
    # If not, get_products will use environment variables
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
        
    all_products = get_products(first=100, shop_domain=os.getenv("SHOPIFY_STORE_DOMAIN"))
    print(f"Fetched {len(all_products)} products from live store.")
    if not all_products:
        print("No products found, check API keys or store domain.")
        return

    print("\n--- Scenario 1: Synonym expansion ('Show me hoodies') ---")
    state1 = ConversationState(session_id="qa-1")
    ans1, filtered1, url1, clar1 = _handle_browse("Show me hoodies", state1, all_products, None)
    print(f"Query: 'Show me hoodies'")
    print(f"Returned {len(filtered1)} products:")
    for p in filtered1[:3]:
        print(f" - {p.get('title')} ({p.get('productType')})")
        
    print("\n--- Scenario 2: Collection matching ('Show me something from summer collection') ---")
    state2 = ConversationState(session_id="qa-2")
    ans2, filtered2, url2, clar2 = _handle_browse("Show me something from summer collection", state2, all_products, None)
    print(f"Query: 'Show me something from summer collection'")
    print(f"Returned {len(filtered2)} products:")
    for p in filtered2[:3]:
        print(f" - {p.get('title')} (Collections: {[c.get('handle') for c in p.get('collections', [])]})")
        
    print("\n--- Scenario 3: Multi-dimension filter ('Blue shirt under 50 in XL') ---")
    state3 = ConversationState(session_id="qa-3")
    ans3, filtered3, url3, clar3 = _handle_browse("Blue shirt under 50 in XL", state3, all_products, None)
    print(f"Query: 'Blue shirt under 50 in XL'")
    print(f"Returned {len(filtered3)} products:")
    for p in filtered3[:3]:
        print(f" - {p.get('title')} ({p.get('productType')}) - {p.get('price')}")
        
    print("\n--- Scenario 4: Deep catalog context ('I want the [product at catalog position 40]') ---")
    if len(all_products) >= 10:
        target_product = all_products[min(40, len(all_products)-1)]
        target_title = target_product.get('title')
        query = f"I want the {target_title}"
        state4 = ConversationState(session_id="qa-4")
        ans4, filtered4, url4, clar4 = _handle_browse(query, state4, all_products, None)
        print(f"Query: '{query}'")
        print(f"Did it find {target_title}? Returned {len(filtered4)} products.")
        for p in filtered4[:3]:
            print(f" - {p.get('title')}")
            
    print("\n--- Scenario 5: Handle resolution ('Add navy XL to cart') ---")
    if filtered3:
        target = filtered3[0]
        # Fake that they selected this product in the previous turn
        state5 = ConversationState(session_id="qa-5")
        state5.stage = "awaiting_variant"
        state5.selected_product = target
        
        # Test add to cart
        ans5, shown5, url5, b1, cart, b2, c1, b3, c2, vid, qty = _handle_awaiting_variant(
            "Add navy XL to cart", state5, all_products, None, "qa-5", None
        )
        print(f"Query: 'Add navy XL to cart' for product {target.get('title')}")
        print(f"Resulting variant ID: {vid}")
        if vid:
            print("Successfully resolved variant.")
        else:
            print(f"Failed to resolve variant. Answer: {ans5}")
            
    print("\nQA complete.")

if __name__ == "__main__":
    asyncio.run(run_qa())
