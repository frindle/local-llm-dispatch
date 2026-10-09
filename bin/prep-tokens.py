#!/usr/bin/env python3

import argparse
import json
import sys
from datetime import datetime

def parse_iso_timestamp(timestamp_str):
    """Parse ISO-8601 timestamp with trailing 'Z'"""
    # Remove the trailing 'Z' and parse
    if timestamp_str.endswith('Z'):
        timestamp_str = timestamp_str[:-1]
    return datetime.fromisoformat(timestamp_str)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--transcript', required=True, help='Path to JSONL transcript file')
    parser.add_argument('--start', required=True, help='Start timestamp (ISO-8601)')
    parser.add_argument('--end', required=True, help='End timestamp (ISO-8601)')
    parser.add_argument('--json', action='store_true', help='Output JSON format')
    
    args = parser.parse_args()
    
    start_time = parse_iso_timestamp(args.start)
    end_time = parse_iso_timestamp(args.end)
    
    prep_input = 0
    prep_output = 0
    turns = 0
    
    try:
        with open(args.transcript, 'r') as f:
            for line_num, line in enumerate(f, 1):
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    # Skip malformed lines
                    continue
                
                # Check if this is an assistant turn and has required fields
                if (data.get('type') == 'assistant' and 
                    'timestamp' in data and 
                    'message' in data and 
                    'usage' in data['message']):
                    
                    try:
                        timestamp = parse_iso_timestamp(data['timestamp'])
                        
                        # Check if timestamp is within window [start, end]
                        if start_time <= timestamp <= end_time:
                            usage = data['message']['usage']
                            
                            # Calculate input tokens (sum of input_tokens, cache_creation_input_tokens, cache_read_input_tokens)
                            input_tokens = usage.get('input_tokens', 0)
                            cache_creation_tokens = usage.get('cache_creation_input_tokens', 0)
                            cache_read_tokens = usage.get('cache_read_input_tokens', 0)
                            
                            prep_input += input_tokens + cache_creation_tokens + cache_read_tokens
                            
                            # Add output tokens
                            prep_output += usage.get('output_tokens', 0)
                            
                            turns += 1
                    except (ValueError, KeyError):
                        # Skip lines with invalid timestamp or missing fields
                        continue
    
    except FileNotFoundError:
        print(f"Error: Transcript file '{args.transcript}' not found", file=sys.stderr)
        sys.exit(1)
    
    prep_total = prep_input + prep_output
    
    if args.json:
        result = {
            "prep_input": prep_input,
            "prep_output": prep_output,
            "prep_total": prep_total,
            "turns": turns,
            "start": args.start,
            "end": args.end
        }
        print(json.dumps(result))
    else:
        print(prep_total)

if __name__ == '__main__':
    main()