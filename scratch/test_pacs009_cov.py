import os
import sys
import json
import asyncio

# Add backend directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.bulk_generator import generate_single_xml
from app.services.validator import ISOValidator

async def main():
    # Initialize validator
    validator = ISOValidator()
    
    # Generate a pacs.009.cov message
    # Selected blocks can include all common elements
    selected_blocks = [
        "instructing_agent",
        "instructed_agent",
        "debtor",
        "creditor",
        "underlying_customer_credit_transfer",
        "payment_type_information",
        "remittance_information"
    ]
    
    print("Generating pacs.009.cov...")
    xml = generate_single_xml("pacs.009.cov", selected_blocks, 1)
    
    print("\nGenerated XML preview (first 1000 chars):")
    print(xml[:1000])
    print("...\n")
    
    print("Validating generated XML with Auto-detect...")
    report = await validator.validate(xml, mode="Full 1-3", message_type="Auto-detect")
    
    print(f"Validation status: {report.status}")
    print(f"Errors count: {report.errors}")
    print(f"Issues found:")
    for issue in report.issues:
        print(f" - Layer {issue.get('layer')}, Code: {issue.get('code')}")
        print(f"   Path: {issue.get('path')}")
        print(f"   Line: {issue.get('line')}")
        print(f"   Msg:  {issue.get('message')}")
        print()

if __name__ == "__main__":
    asyncio.run(main())
