import os, json

from archivebot.utility import SlackMessage

class JsonFiles():
  """ File-based storage for Slack ArchiveBot """

  def __init__(self, base_dir: str) -> None:
    """ Initializes the JsonFiles class

    Args:
        base_dir (str): Base directory for the storage
    """
    self.base_dir = base_dir
    self._setup()


  def _setup(self) -> None:
    """ Creates the base directory and message directory """
    os.makedirs(self.base_dir, exist_ok=True)
    os.makedirs(f"{self.base_dir}/messages", exist_ok=True)


  def _get_message_path(self, msg: SlackMessage) -> str:
    """ Builds the path to the messages file, based on the channel and timestamp

    Args:
        msg (SlackMessage): The message to build the path for

    Returns:
        str: The path to the messages json file
    """
    return f"{self.base_dir}/messages/{msg.channel_id()}/{msg.iso_date()}.json"


  def save_message(self, message: SlackMessage) -> None:
    """ Saves the message to the file storage

    Args:
        message (SlackMessage): The message to save
    """
    # Ensure the message directory exists
    file_path = self._get_message_path(message)
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    # Create empty file if it doesn't exist
    if not os.path.isfile(file_path) or os.stat(file_path).st_size == 0:
      with open(file_path, 'w+') as f:
        json.dump({}, f)
        f.close()

    # Append the message to the file
    with open(file_path, 'r+') as f:
      data = json.load(f)
      # Insert or update the message
      data[message.uuid()] = message.raw()
      f.seek(0)
      json.dump(data, f)
