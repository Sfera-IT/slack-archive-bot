import argparse
import logging
import os
import time
from sentence_transformers import SentenceTransformer
import numpy as np
from utils import db_connect

# Setup argument parser
parser = argparse.ArgumentParser()
parser.add_argument(
    "-d",
    "--database-path",
    default="slack.sqlite",
    help="path to the SQLite database. (default = ./slack.sqlite)",
)
parser.add_argument(
    "-l",
    "--log-level",
    default="INFO",
    help="CRITICAL, ERROR, WARNING, INFO or DEBUG (default = INFO)",
)
parser.add_argument(
    "-b",
    "--batch-size",
    type=int,
    default=100,
    help="Number of messages to process in each batch (default = 100)",
)
args = parser.parse_args()

# Setup logging
log_level = args.log_level.upper()
assert log_level in ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]
logging.basicConfig(
    level=getattr(logging, log_level),
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load the SentenceTransformer model
model = SentenceTransformer('paraphrase-MiniLM-L6-v2')

def create_embeddings(message):
    try:
        embeddings = model.encode(message)
        return embeddings.tobytes()
    except Exception as e:
        logger.error(f"Error creating embedding for message: {e}")
        return None

def update_embeddings():
    conn, cursor = db_connect(args.database_path)
    
    try:
        # Get total count of messages with null embeddings
        cursor.execute("SELECT COUNT(*) FROM messages WHERE embeddings IS NULL")
        total_null_embeddings = cursor.fetchone()[0]
        logger.info(f"Total messages with null embeddings: {total_null_embeddings}")

        processed_count = 0
        start_time = time.time()

        while True:
            # Fetch a batch of messages with null embeddings, ordered by timestamp DESC
            cursor.execute("""
                SELECT message, timestamp FROM messages 
                WHERE embeddings IS NULL 
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (args.batch_size,))
            
            messages = cursor.fetchall()
            
            if not messages:
                break  # No more messages to process

            for message, timestamp in messages:
                embedding = create_embeddings(message)
                if embedding:
                    cursor.execute("""
                        UPDATE messages 
                        SET embeddings = ? 
                        WHERE timestamp = ? AND embeddings IS NULL
                    """, (embedding, timestamp))
                    
                    processed_count += 1
                    
                    if processed_count % 100 == 0:
                        elapsed_time = time.time() - start_time
                        progress = (processed_count / total_null_embeddings) * 100
                        logger.info(f"Processed {processed_count}/{total_null_embeddings} messages ({progress:.2f}%). Elapsed time: {elapsed_time:.2f} seconds")

            conn.commit()
            logger.debug(f"Committed batch of {len(messages)} messages")

        logger.info(f"Finished processing. Total messages updated: {processed_count}")

    except Exception as e:
        logger.error(f"An error occurred: {e}")

    finally:
        conn.close()

if __name__ == "__main__":
    logger.info("Starting embeddings update process")
    update_embeddings()
    logger.info("Embeddings update process completed")