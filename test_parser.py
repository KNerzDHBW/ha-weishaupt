import os
import sys

# Add custom_components to path
sys.path.append(os.path.join(os.getcwd(), 'custom_components'))

try:
    from wem_webinterface.parser import parse_settings_page
except ImportError as e:
    print(f"Error importing parse_settings_page: {e}")
    sys.exit(1)

html_path = os.path.join('_inspect', 'stack4.html')
if not os.path.exists(html_path):
    print(f"File not found: {html_path}")
    sys.exit(1)

with open(html_path, 'r', encoding='utf-8') as f:
    html_content = f.read()

parameters = parse_settings_page(html_content, 'stack4')

if parameters is None:
    print("No parameters found or error parsing.")
else:
    print(f"{'Name':<30} | {'Value':<10} | {'Type':<10} | {'Unit':<5} | {'Readonly':<8}")
    print("-" * 75)
    for p in parameters:
        # Assuming ParsedParameter object has attributes: name, value, type, unit, readonly
        # Based on typical home assistant integration structure
        print(f"{getattr(p, 'name', 'N/A'):<30} | {getattr(p, 'value', 'N/A'):<10} | {getattr(p, 'type', 'N/A'):<10} | {getattr(p, 'unit', 'N/A'):<5} | {getattr(p, 'readonly', 'N/A'):<8}")
