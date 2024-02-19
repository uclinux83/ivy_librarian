from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from openai import OpenAI
import pandas as pd
import time
from datetime import datetime
import requests
import json

SLACK_SOCKET_TOKEN = "Your Slack Socket token, starting with xapp-..."
SLACK_BOT_USER_TOKEN = "Your Slack bot user token, starting with xoxb-"

WAITING_MESSAGE = "Please wait..."

OPENAI_KEY = "Your OpenAI API key"
GPT_MODEL = "gpt-4"

DATABASE_FILE = "library.csv"
LOG_FILE = "log.csv"
AVAILABLE_STATUS = "available"
BORROWED_STATUS = "borrowed"

SYSTEM_PROMPT = """You are Ivy, a virtual librarian.
You can help the user to borrow book, return book or get information about books that you have in the library.
To borrow or return a book, the user needs to provide the ID of the book. Book ID is a string starting with SF that can be found on a label on the cover of the book.
It is OK to share with the user who borrowed a specific book.
Basing on the user's request, you call the corresponding function to borrow book, return book, or get information about books in the library.
Do not answer questions that are not related to books or library.
"""

app = App(token = SLACK_BOT_USER_TOKEN)
ai_client = OpenAI(api_key = OPENAI_KEY)

tools = [
    {
        "type": "function",
        "function": {
            "name": "borrow_book",
            "description": "Borrow a book from the library",
            "parameters": {
                "type": "object",
                "properties": {
                    "book_id": {
                        "type": "string",
                        "description": "ID of the book",
                    }
                },
                "required": ["book_id"]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "return_book",
            "description": "Return a book to the library",
            "parameters": {
                "type": "object",
                "properties": {
                    "book_id": {
                        "type": "string",
                        "description": "ID of the book",
                    }
                },
                "required": ["book_id"]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_book_information",
            "description": "Provide information about books in the library",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Question that user ask regarding to information about books in the library",
                    }
                },
                "required": ["question"]
            },
        },
    }
]

@app.message()
def im_message(client, message):
    if message["channel_type"] == "im": # Direct message
        reply = client.chat_postMessage(channel = message["channel"], thread_ts = message["ts"], text = WAITING_MESSAGE)
        response = process_conversation(client, message)
        client.chat_update(channel = message["channel"], ts = reply["ts"], text = response)
        
@app.event("app_mention")
def handle_app_mention_events(client, body):
    message = body["event"]
    reply = client.chat_postMessage(channel = message["channel"], thread_ts = message["ts"], text = WAITING_MESSAGE)
    response = process_conversation(client, message)
    client.chat_update(channel = message["channel"], ts = reply["ts"], text = response)

#============================================#
def process_conversation(client, message):
    result = get_gpt_response(client, message)
    if result.content:
        response = result.content
    elif result.tool_calls:
        function_name = result.tool_calls[0].function.name
        arguments = json.loads(result.tool_calls[0].function.arguments)
        slack_id = message["user"]
        if function_name == "borrow_book":
            book_id = arguments["book_id"]
            user_info = client.users_info(user=slack_id)
            name = user_info["user"]["profile"]["real_name"] if user_info["user"]["profile"]["real_name"] else slack_id
            response = borrow_book(book_id, slack_id, name)
        elif function_name == "return_book":
            book_id = arguments["book_id"]
            response = return_book(book_id, slack_id)
        elif function_name == "get_book_information":
            question = arguments["question"]
            response = get_book_information(question)
        else:
            response = f"[ERROR] Invalid function"
    else:
        response = f"[ERROR] Invalid response from OpenAI"
    return response

def get_gpt_response(client, message):
    conversation_history = get_conversation_history(client, message)
    prompt_structure = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg in conversation_history:
        prompt_structure.append(msg) 
    try:
        response = ai_client.chat.completions.create(
            model = GPT_MODEL,
            messages = prompt_structure,
            tools = tools,
            tool_choice = "auto"
        )
        return response.choices[0].message
    except:
        return f"[ERROR] Problem calling OpenAI API"

def get_conversation_history(client, message):
    result = []
    if "thread_ts" in message:
        conversation = client.conversations_replies(channel = message["channel"], ts = message["thread_ts"])
        if "messages" in conversation:
            for msg in conversation["messages"]:
                if "client_msg_id" in msg:
                    result.append({"role": "user", "content": msg["text"]})
                if "bot_id" in msg:
                    if msg["text"] != WAITING_MESSAGE:
                        result.append({"role": "assistant", "content": msg["text"]})
    else:
        result.append({"role": "user", "content": message["text"]})
    return result

def borrow_book(book_id, borrower_id, borrower_name):
    try:
        df = pd.read_csv(DATABASE_FILE)
        matching_index = df.index[df['book_id'] == book_id.upper()].tolist()
        if matching_index:
            if df.at[matching_index[0], 'status'] == AVAILABLE_STATUS:
                df.at[matching_index[0], 'status'] = BORROWED_STATUS
                df.at[matching_index[0], 'borrower_id'] = borrower_id
                df.at[matching_index[0], 'borrower_name'] = borrower_name
                df.at[matching_index[0], 'borrowed_date'] = datetime.now().strftime("%d %b %Y")
                df.to_csv(DATABASE_FILE, index=False)
                
                #Log the borrow activity
                write_to_log(f"borrow,{df.at[matching_index[0], 'book_id']},{df.at[matching_index[0], 'borrower_id']},{df.at[matching_index[0], 'borrower_name']},{df.at[matching_index[0], 'borrowed_date']}\n")
                return f"[SUCCESS] Your book (ID: {book_id}) has been borrowed successfully. Please return it within 14 days. Enjoy your reading, {borrower_name}!"
            else:
                return f"[ERROR] Book with ID {book_id.upper()} is not available. It is currently borrowed by {df.at[matching_index[0], 'borrower_name']}"
        else:
            return f"[ERROR] Book with ID {book_id.upper()} can not be found in the library"
    except Exception as e:
        return f"[ERROR] Problem updating database file"

def return_book(book_id, current_user_id):
    try:
        df = pd.read_csv(DATABASE_FILE)
        matching_index = df.index[df['book_id'] == book_id.upper()].tolist()
        if matching_index:
            if df.at[matching_index[0], 'status'] == BORROWED_STATUS:
                if df.at[matching_index[0], 'borrower_id'] == current_user_id:
                    df.at[matching_index[0], 'status'] = AVAILABLE_STATUS
                    df.at[matching_index[0], 'borrower_id'] = ""
                    df.at[matching_index[0], 'borrower_name'] = ""
                    df.at[matching_index[0], 'borrowed_date'] = ""

                    df.to_csv(DATABASE_FILE, index=False)
                    #Log the return activity
                    write_to_log(f"return,{df.at[matching_index[0], 'book_id']},{current_user_id},{current_user_id},{datetime.now().strftime('%d %b %Y')}\n")
                    return f"[SUCCESS] Your book (ID: {book_id}) has been returned successfully. Thank you!"
                else:
                    return f"[ERROR] You are not the current borrower of book with ID {book_id.upper()}. The book is currently borrowed by {df.at[matching_index[0], 'borrower_name']}"
            else:
                return f"[ERROR] Book with ID {book_id.upper()} is not currently borrowed"
        else:
            return f"[ERROR] Book with ID {book_id.upper()} can not be found in the library"
    except Exception as e:
        return f"[ERROR] Problem updating database file"

def get_book_information(question):
    with open(DATABASE_FILE, 'r') as file:
        file_content = file.read()
    
    system_prompt = f"You are a virtual librarian. You answer question basing on the book information that you have in the database. Here is the book information that you have in CSV format:\n{file_content}"
    
    messages = [{"role": "system", "content": system_prompt},
               {"role": "user", "content": question}]
    try:
        response = ai_client.chat.completions.create(model = GPT_MODEL, messages = messages)
        return response.choices[0].message.content
    except Exception as e:
        return f"[ERROR] Problem calling OpenAI API"

def write_to_log(text):
    with open(LOG_FILE, 'a') as file:
        file.write(text)
    
#============================================#
# Start the bot
if __name__ == "__main__":
    SocketModeHandler(app, SLACK_SOCKET_TOKEN).start()
