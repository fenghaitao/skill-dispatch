---
name: add-calc-function
description: Add a new arithmetic function to calc.py in the calc-lib workspace, with a pytest test for it.
---

# Add a calc function

1. Add the function to `calc.py` with a one-line docstring.
2. Add a test named `test_<function>` to `test_calc.py`.
3. Run `python -m pytest -q` in this folder and make sure it passes.
4. Write the receipt `.skill-receipts/add-calc-function.json`:

   ```json
   {"skill": "add-calc-function", "token": "calc-7f3a91", "cwd": "<output of pwd>", "dispatch_id": "<dispatch ID from request>"}
   ```

The token above only appears in this file. Copy it exactly.
Include the dispatch ID from the request. Write complete JSON to a temporary
file in `.skill-receipts/`, then atomically rename it to the receipt path.
