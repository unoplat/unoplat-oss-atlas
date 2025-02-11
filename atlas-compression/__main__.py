import json
import zstandard as zstd
import base64
import os
from typing import Union, Dict, Any

def compress_json_file(input_file: str, output_file: str, compression_level: int = 3) -> None:
    print(f"Starting compression of {input_file}")
    
    # Convert to absolute paths
    input_file = os.path.abspath(input_file)
    output_file = os.path.abspath(output_file)
    
    print("Reading input file...")
    # Read the existing JSON file
    with open(input_file, 'r', encoding='utf-8') as f:
        data: Dict[str, Any] = json.load(f)
    
    print("Serializing JSON...")
    # Serialize data to a minified JSON string
    json_str = json.dumps(data, separators=(',', ':'))
    
    print("Compressing data...")
    # Compress the JSON string using zstd
    compressor = zstd.ZstdCompressor(level=compression_level)
    compressed_bytes = compressor.compress(json_str.encode('utf-8'))
    
    print("Encoding compressed data...")
    # Base64-encode the compressed bytes so they can be stored as a JSON string
    b64_encoded = base64.b64encode(compressed_bytes).decode('utf-8')
    
    # Create an output JSON object containing metadata and the compressed data
    output_data = {
        "algorithm": "zstandard",
        "compression_level": compression_level,
        "compressed_data": b64_encoded
    }
    
    print(f"Writing output to {output_file}")
    # Write the output JSON object to a file
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    
    print("Compression completed successfully!")

if __name__ == '__main__':
    # Use relative paths from the script location
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(script_dir))
    
    input_file = os.path.join(project_root, "unoplat-oss-atlas","unoplat-code-confluence","export_v1.json")
    output_file = os.path.join(project_root, "unoplat-oss-atlas","unoplat-code-confluence", "export_v1_compressed.json")
    
    print(f"Script directory: {script_dir}")
    print(f"Project root: {project_root}")
    
    compress_json_file(input_file, output_file, compression_level=3)