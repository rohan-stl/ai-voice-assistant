import os

def refactor():
    with open('main.py', 'r') as f:
        lines = f.readlines()
        
    start_idx = 531 # 0-indexed line 532
    end_idx = 1247  # 0-indexed line 1248 (just before _build_precise_product_url)
    
    extracted_lines = lines[start_idx:end_idx]
    
    header = """import re
from typing import Optional
from conversation_state import ConversationState

WORD_TO_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "a": 1, "an": 1
}

"""
    
    with open('variant_resolver.py', 'w') as f:
        f.write(header)
        f.writelines(extracted_lines)
        
    new_main_lines = lines[:start_idx] + ["from variant_resolver import *\\n"] + lines[end_idx:]
    
    with open('main.py', 'w') as f:
        f.writelines(new_main_lines)

if __name__ == '__main__':
    refactor()
