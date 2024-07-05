import argparse
import logging
import os
import traceback
import shlex


from slack_bolt import App

from utils import db_connect, migrate_db

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
    default="debug",
    help="CRITICAL, ERROR, WARNING, INFO or DEBUG (default = DEBUG)",
)
parser.add_argument(
    "-p", "--port", default=3333, help="Port to serve on. (default = 3333)"
)
cmd_args, unknown = parser.parse_known_args()

# Check the environment too
log_level = os.environ.get("ARCHIVE_BOT_LOG_LEVEL", cmd_args.log_level)
database_path = os.environ.get("ARCHIVE_BOT_DATABASE_PATH", cmd_args.database_path)
port = os.environ.get("ARCHIVE_BOT_PORT", cmd_args.port)

# Setup logging
log_level = log_level.upper()
assert log_level in ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]
logging.basicConfig(level=getattr(logging, log_level))
logger = logging.getLogger(__name__)

app = App(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET"),
    logger=logger,
)

# Save the bot user's user ID
app._bot_user_id = app.client.auth_test()["user_id"]


# Uses slack API to get most recent user list
# Necessary for User ID correlation
def update_users(conn, cursor):
    logger.info("Updating users")
    info = app.client.users_list()

    args = []
    for m in info["members"]:
        name = m["profile"]["display_name"]
        if not name:
            name = m["profile"]["real_name"]
        args.append(
            (
                name,
                m["id"],
                m["profile"].get(
                    "image_72",
                    "http://fst.slack-edge.com/66f9/img/avatars/ava_0024-32.png",
                ),
            )
        )
    cursor.executemany("INSERT INTO users(name, id, avatar) VALUES(?,?,?)", args)
    conn.commit()


def get_channel_info(channel_id):
    channel = app.client.conversations_info(channel=channel_id)["channel"]

    # Get a list of members for the channel. This will be used when querying private channels.
    response = app.client.conversations_members(channel=channel["id"])
    members = response["members"]
    while response["response_metadata"]["next_cursor"]:
        response = app.client.conversations_members(
            channel=channel["id"], cursor=response["response_metadata"]["next_cursor"]
        )
        members += response["members"]

    return (
        channel["id"],
        channel["name"],
        channel["is_private"],
        [(channel["id"], m) for m in members],
    )


def update_channels(conn, cursor):
    logger.info("Updating channels")
    channels = app.client.conversations_list(types="public_channel,private_channel")[
        "channels"
    ]

    channel_args = []
    member_args = []
    for channel in channels:
        if channel["is_member"]:
            channel_id, channel_name, channel_is_private, members = get_channel_info(
                channel["id"]
            )

            channel_args.append((channel_name, channel_id, channel_is_private))

            member_args += members

    cursor.executemany(
        "INSERT INTO channels(name, id, is_private) VALUES(?,?,?)", channel_args
    )
    cursor.executemany("INSERT INTO members(channel, user) VALUES(?,?)", member_args)
    conn.commit()


def handle_query(event, cursor, say):
    say("Questa interfaccia è stata disattivata. Ora puoi andare qui: https://sferaarchive-client.vercel.app/")
    return
    """
    Handles a DM to the bot that is requesting a search of the archives.

    Usage:

        <query> from:<user> in:<channel> sort:asc|desc limit:<number>

        query: The text to search for.
        user: If you want to limit the search to one user, the username.
        channel: If you want to limit the search to one channel, the channel name.
        sort: Either asc if you want to search starting with the oldest messages,
            or desc if you want to start from the newest. Default asc.
        limit: The number of responses to return. Default 10.

    Special Commands (not returned in usage_text):
        inactive:N Returns the users inactive in the last N days (no messages sent). Example inactive:30
        topusers:N Shows a list of the most active users (number of messages sent) on the last N days
    """
    try:
        usage_text= "*Usage*:\n\n\t"\
                    "<query> from:<user> in:<channel> sort:asc|desc limit:<number>\n\n\n*"\
                    "NOTE*: \n\n 1) the BOT search all the terms, if you want to search for the exact phrase use quotes around the 'search terms' \n\n"\
                    "2) if your search term contains quotes, escape it with a \\ slash before, like this: I\\'m \n\n\n"\
                    "*Params*\n\n\t"\
                    "query: The text to search for.\n\t"\
                    "user: If you want to limit the search to one user, the username. For space separated nicknames, use double quotes in this way 'from:\"name surname\" query' \n\t"\
                    "channel: If you want to limit the search to one channel, the channel name.\n\t"\
                    "sort: Either asc if you want to search starting with the oldest messages, or desc if you want to start from the newest. Default asc.\n\t"\
                    "limit: The number of responses to return. Default 10.\n\n\n"\
                    "*Special Commands*\n\n\t"\
                    "!topusers:N Shows a list of the most active users (number of messages sent) on the last N days + bonus info :-) \n\t"\
                    "!inactive:N Shows a list of the inactive users (no messages) on the last N days"

        text = []
        user_name = None
        channel_name = None
        sort = None
        limit = 10

        s = event["text"].lower()
        
        # split except when surrounded by quotes
        # john doe is splitted in 'john' and 'doe'
        # 'john doe' is splitted in 'john doe'
        params = shlex.split(s)

        if len(params) == 1:
            if params[0] == "!help":
                say(usage_text)
                return None

        for p in params:
            # Handle emoji
            # usual format is " :smiley_face: "
            if len(p) > 2 and p[0] == ":" and p[-1] == ":":
                text.append(p)
                continue

            p = p.split(":")

            if len(p) == 1:
                text.append(p[0])
            if len(p) == 2:
                # workaround: since url contains colons ":" the split interpret it as a parameter
                # so we re-assemble it
                if p[0] in ['<http', '<https', 'http', 'https']:
                    text.append(p[0]+":"+p[1])
                if p[0] == "from":
                    user_name = p[1]
                if p[0] == "in":
                    channel_name = p[1].replace("#", "").strip()
                if p[0] == "sort":
                    if p[1] in ["asc", "desc"]:
                        sort = p[1]
                    else:
                        raise ValueError("Invalid sort order %s" % p[1])
                if p[0] == "limit":
                    try:
                        limit = int(p[1])
                    except:
                        raise ValueError("%s not a valid number" % p[1])
                # if p[0] == "maintenance":
                #     say(maintenance(p[1]))
                #     return
                if p[0] == "!inactive":
                    say(inactive(p[1]))
                    return
                if p[0] == "!topusers":
                    say(topusers(p[1]))
                    return
                # if p[0] == "oblivion":
                #     say(oblivion(p[1]))

        query = f"""
            SELECT DISTINCT
                messages.message, messages.user, messages.timestamp, messages.channel, messages.permalink
            FROM messages
            INNER JOIN users ON messages.user = users.id
            -- Only query channel that archive bot is a part of
            INNER JOIN (
                SELECT * FROM channels
                INNER JOIN members ON
                    channels.id = members.channel AND
                    members.user = (?)
            ) as channels ON messages.channel = channels.id
            INNER JOIN members ON channels.id = members.channel
            WHERE
                -- Only return messages that are in public channels or the user is a member of
                (channels.is_private <> 1 OR members.user = (?)) AND
                
        """

        # Search for each search term in any order, duplicating the LIKE clause for each text element
        text = ['%'+item+'%' for item in text]
        elements = len(text)
        message_str = 'messages.message LIKE (?) AND ' * elements
        # remove last AND
        message_str = message_str[:len(message_str) - 5]
        # concatenate the clause
        query = query + message_str
        query_args = [app._bot_user_id, event["user"]]
        # add the arguments for the parametrized query
        query_args.extend(text)

        if elements == 0:
            query += "1 "

        if user_name:
            query += " AND users.name LIKE (?)"
            query_args.append(user_name)
        if channel_name:
            query += " AND channels.name = (?)"
            query_args.append(channel_name)
        if sort:
            query += " ORDER BY messages.timestamp %s" % sort

        logger.debug(query)
        logger.debug(query_args)

        cursor.execute(query, query_args)

        res = cursor.fetchmany(limit)
        cursor.close()
        res_message = None
        if res:
            res = tuple(map(get_permalink_and_save, res))
            logger.debug("debugging res")
            logger.debug(res)
            res_message = "\n".join(
                [
                    "*<@%s>* _<!date^%s^{date_pretty} {time}|A while ago>_ _<#%s>_\n%s\n_[Permalink](%s)_\n\n"
                    % (i[1], int(float(i[2])), i[3], quote_message(i[0]), i[4])
                    for i in res
                ]
            )
        if res_message:
            # replace everyone tag breaking everything
            res_message = res_message.replace("<!everyone>", "everyone")
            say(res_message)
        else:
            say("No results found\n\n"+usage_text)
    except ValueError as e:
        logger.error(traceback.format_exc())
        say(str(e))

def maintenance(msg: str) -> str:
    if msg == "delete_permalinks":
        conn, cursor = db_connect(database_path)
        cursor.execute("update messages set permalink = ''")
        conn.commit()
        return "permalinks deleted"
    return "no maintenance executed"

def inactive(days: str) -> str:
    try:                
        days = str(int(days))
    except:
        raise ValueError("%s not a valid number" % days)

    conn, cursor = db_connect(database_path)
    query = "select group_concat(name, ', ') from users where id not in (select distinct user from messages where datetime(timestamp, 'unixepoch') > date('now', ?) order by timestamp desc)";
    query_args = ['-'+days+' days']
    cursor.execute(query, query_args)
    res = cursor.fetchone()
    if len(res) > 0:
        return res[0]
    else:
        return "Something went wrong"


def topusers(days: str) -> str:
    try:                
        days = str(int(days))
    except:
        raise ValueError("%s not a valid number" % days)
    
    conn, cursor = db_connect(database_path)
    query = f"""    
    SELECT group_concat(name || ': ' || messaggi || ', rants: ' || rants || ' praises: ' || praises, '\n') FROM (
        SELECT 
            users.name,
            count(*) messaggi, 
            SUM(IIF(channels.name = "rants", 1, 0)) rants,
            SUM(IIF(channels.name = "praise", 1, 0)) praises
        FROM users 
        INNER JOIN messages 
            ON users.id = messages.user 
        INNER JOIN channels 
            ON channels.id = messages.channel 
        WHERE 
            datetime(timestamp, 'unixepoch') > date('now', ?)
        GROUP BY users.name 
        ORDER BY count(*) DESC
    ) AS t1;

            """
    query_args = ['-'+days+' days']
    cursor.execute(query, query_args)
    res = cursor.fetchone()
    if len(res) > 0:
        return res[0]
    else:
        return "Something went wrong"

def oblivion(msg: str) -> str:
    if msg != "confirm":
        return "This command WILL DELETE ALL YOUR POST IN THE DATABASE of the BOT, and it will be impossible to recover them again. If you are sure, repeat the command again using oblivion:confirm"
    else:
        conn, cursor = db_connect(database_path)
        # cursor.execute("update messages set permalink = ''")
        # conn.commit()
        return "Chi sei?"

def quote_message(msg: str) -> str:
    """
    Prefixes each line with a '>'.

    In makrdown this symbol denotes a quote, so Slack will render the message
    wrapped in a blockquote tag.
    """
    return "> ".join(msg.splitlines(True))

def get_first_reply_in_thread(res):
    # get all ther replies of the message
    try:
        replies = app.client.conversations_replies(channel=res[3], ts=res[2])
        # if we have at least one reply
        if len(replies.data["messages"]) > 0:
            # if the timestamp of the actual message is equal to thread_ts of the first message in the replies, it means 
            # that it's the main (parent) message.
            if "thread_ts" in replies.data["messages"][0]:
                if res[2] == replies.data["messages"][0]["thread_ts"]:
                    # since main (parent) message cannot be referenced via permalink in Slack Free, we point the permalink 
                    # to the first child
                    if len(replies.data["messages"]) > 1:
                        # get the timestamp of the first reply and replace the link to it
                        reslist = list(res)
                        reslist[2] = replies.data["messages"][1]["ts"]
                        res = tuple(reslist)
    except Exception as e:
        logger.debug("An error occurred fetching replies: ", e)

    return res

def get_permalink_and_save(res):
    if res[4] == "":
        newres = get_first_reply_in_thread(res)
        logger.debug("Getting Permalink for res: ")
        logger.debug(res)
        conn, cursor = db_connect(database_path)

        permalink = app.client.chat_getPermalink(channel=newres[3], message_ts=newres[2])
        logger.debug(permalink["permalink"])
        res = res[:-1]
        res = res + (permalink["permalink"],)

        cursor.execute(
            "UPDATE messages SET permalink = ? WHERE user = ? AND channel = ? AND timestamp = ?",
            (permalink["permalink"], res[1], res[3], res[2]),
        )
        conn.commit()
    else:
        logger.debug("Permalink already in database, skipping get_permalink_and_save")

    return res


@app.event("member_joined_channel")
def handle_join(event):
    conn, cursor = db_connect(database_path)

    # If the user added is archive bot, then add the channel too
    if event["user"] == app._bot_user_id:
        channel_id, channel_name, channel_is_private, members = get_channel_info(
            event["channel"]
        )
        cursor.execute(
            "INSERT INTO channels(name, id, is_private) VALUES(?,?,?)",
            (channel_name, channel_id, channel_is_private),
        )
        cursor.executemany("INSERT INTO members(channel, user) VALUES(?,?)", members)
    else:
        cursor.execute(
            "INSERT INTO members(channel, user) VALUES(?,?)",
            (event["channel"], event["user"]),
        )

    conn.commit()


@app.event("member_left_channel")
def handle_left(event):
    conn, cursor = db_connect(database_path)
    cursor.execute(
        "DELETE FROM members WHERE channel = ? AND user = ?",
        (event["channel"], event["user"]),
    )
    conn.commit()


def handle_rename(event):
    channel = event["channel"]
    conn, cursor = db_connect(database_path)
    cursor.execute(
        "UPDATE channels SET name = ? WHERE id = ?", (channel["name"], channel["id"])
    )
    conn.commit()


@app.event("channel_rename")
def handle_channel_rename(event):
    handle_rename(event)


@app.event("group_rename")
def handle_group_rename(event):
    handle_rename(event)


# For some reason slack fires off both *_rename and *_name events, so create handlers for them
# but don't do anything in the *_name events.
@app.event({"type": "message", "subtype": "group_name"})
def handle_group_name():
    pass


@app.event({"type": "message", "subtype": "channel_name"})
def handle_channel_name():
    pass


@app.event("user_change")
def handle_user_change(event):
    user_id = event["user"]["id"]
    new_username = event["user"]["profile"]["display_name"]
    if not new_username:
        new_username = event["user"]["profile"]["real_name"]

    conn, cursor = db_connect(database_path)
    cursor.execute("UPDATE users SET name = ? WHERE id = ?", (new_username, user_id))
    conn.commit()


def handle_message(message, say):
    logger.debug(message)
    if "text" not in message or message["user"] == "USLACKBOT":
        return

    conn, cursor = db_connect(database_path)

    # If it's a DM, treat it as a search query
    if message["channel_type"] == "im":
        handle_query(message, cursor, say)
    elif "user" not in message:
        logger.warning("No valid user. Previous event not saved")
    else:  # Otherwise save the message to the archive.
        # get the permalink only if the message is not the main post (slack bug), otherwise leave it empty
        if "thread_ts" in message:
            permalink = app.client.chat_getPermalink(
                channel=message["channel"], message_ts=message["ts"]
            )
        else:
            permalink = {'permalink': ''}

        # Check if user opted out
        cursor.execute("SELECT user, timestamp FROM optout WHERE user = ?", (message["user"],))
        row = cursor.fetchone()

        if row is not None:
            message["text"] = "User opted out of archiving. This message has been deleted"
            message["user"] = "USLACKBOT"
            message["permalink"] = ""

        logger.debug(permalink["permalink"])
        cursor.execute(
            "INSERT INTO messages VALUES(?, ?, ?, ?, ?, ?)",
            (
                message["text"],
                message["user"],
                message["channel"],
                message["ts"],
                permalink["permalink"],
                message["thread_ts"] if "thread_ts" in message else None,
            ),
        )
        conn.commit()

        # Ensure that the user exists in the DB
        cursor.execute("SELECT * FROM users WHERE id = ?", (message["user"],))
        row = cursor.fetchone()
        if row is None:
            update_users(conn, cursor)

    logger.debug("--------------------------")


@app.message("")
def handle_message_default(message, say):
    handle_message(message, say)


@app.event({"type": "message", "subtype": "thread_broadcast"})
def handle_message_thread_broadcast(event, say):
    handle_message(event, say)


@app.event({"type": "message", "subtype": "message_changed"})
def handle_message_changed(event):
    message = event["message"]
    conn, cursor = db_connect(database_path)
    cursor.execute(
        "UPDATE messages SET message = ? WHERE user = ? AND channel = ? AND timestamp = ?",
        (message["text"], message["user"], event["channel"], message["ts"]),
    )
    conn.commit()

@app.event("channel_created")
def handle_channel_created(event):
    channel_id = event["channel"]["id"]
    channel_is_private = app.client.conversations_info(channel=channel_id)["channel"]["is_private"]

    if channel_is_private is False:
        logger.debug("Channel id %s is public, joining", channel_id)
        app.client.conversations_join(channel=channel_id)

def init():
    # Initialize the DB if it doesn't exist
    conn, cursor = db_connect(database_path)
    migrate_db(conn, cursor)

    # Update the users and channels in the DB and in the local memory mapping
    try:
        update_users(conn, cursor)
        update_channels(conn, cursor)
    except Exception as e:
        logger.error("Error updating users and channels: %s" % e)
        


def main():
    init()

    # Start the development server
    app.start(port=port)


if __name__ == "__main__":
    main()
    