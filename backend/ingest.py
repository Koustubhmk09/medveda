import os
import time
from src.helper import download_hugging_face_embeddings, load_pdf_file, text_split, generate_ids
from langchain_pinecone import PineconeVectorStore
from pinecone import Pinecone
from dotenv import load_dotenv

load_dotenv()

PINECONE_API_KEY = os.environ.get('PINECONE_API_KEY')
PINECONE_INDEX_NAME = os.environ.get('PINECONE_INDEX_NAME')

def ingest_data(clear_index=False):
    # 1. Configuration for the 3 major books
    books_config = [
        {
            "filename": "The_Merck_Manual_of_Diagnosis_and_Therapy_19th Edition.pdf",
            "source": "Merck Manual 19th Ed",
            "knowledge_type": "clinical_knowledge"
        },
        {
            "filename": "Medical_book.pdf",
            "source": "GALE Encyclopedia of Medicine",
            "knowledge_type": "clinical_knowledge"
        },
        {
            "filename": "Drug Guide for Nurses.pdf",
            "source": "Davis Drug Guide 11th Ed",
            "knowledge_type": "medicine_intelligence"
        }
    ]

    # 4. Download Embeddings
    print("Downloading embeddings model...")
    embeddings = download_hugging_face_embeddings()
    
    # 5. Connect to Pinecone
    pc = Pinecone(api_key=PINECONE_API_KEY)
    index = pc.Index(PINECONE_INDEX_NAME)
    
    if clear_index:
        print(f"Clearing all existing data from index: {PINECONE_INDEX_NAME}...")
        index.delete(delete_all=True)

    vectorstore = PineconeVectorStore(
        index=index,
        embedding=embeddings,
        pinecone_api_key=PINECONE_API_KEY
    )

    for config in books_config:
        file_path = os.path.join("data", config["filename"])
        if not os.path.exists(file_path):
            print(f"Warning: {config['filename']} not found in data/ folder. Skipping...")
            continue

        print(f"\nProcessing {config['source']}...")
        
        # 1. Load Data
        extracted_data = load_pdf_file(file_path)
        
        # 2. Split Data
        text_chunks = text_split(extracted_data)
        
        # 3. Add Metadata
        for chunk in text_chunks:
            chunk.metadata["source"] = config["source"]
            chunk.metadata["knowledge_type"] = config["knowledge_type"]
            
        # 4. Generate Unique IDs for Deduplication
        ids = generate_ids(text_chunks)
        
        # 5. Push to Pinecone in batches
        print(f"Pushing {len(text_chunks)} chunks to Pinecone...")
        
        batch_size = 100
        for i in range(0, len(text_chunks), batch_size):
            batch_chunks = text_chunks[i:i + batch_size]
            batch_ids = ids[i:i + batch_size]
            
            max_retries = 3
            retry_delay = 5
            for attempt in range(max_retries):
                try:
                    vectorstore.add_documents(documents=batch_chunks, ids=batch_ids)
                    break
                except Exception as e:
                    if attempt < max_retries - 1:
                        print(f"Error uploading batch: {e}. Retrying...")
                        time.sleep(retry_delay)
                        retry_delay *= 2
                    else:
                        raise e
            
            print(f"Uploaded {min(i + batch_size, len(text_chunks))}/{len(text_chunks)}")

    print("\nIngestion and cleaning completed successfully!")


if __name__ == "__main__":
    if not PINECONE_API_KEY or not PINECONE_INDEX_NAME:
        print("Error: Please set PINECONE_API_KEY and PINECONE_INDEX_NAME in your .env file.")
    else:
        # Ask user if they want to clear the index first to remove old duplicates
        choice = input("Do you want to clear the existing index before ingestion? (y/n): ").lower()
        should_clear = True if choice == 'y' else False
        ingest_data(clear_index=should_clear)
