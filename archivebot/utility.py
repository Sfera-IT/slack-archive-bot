

def message_uuid(timestamp: str, channel: str) -> str:
  """ Generates a message UUID

  Ref: https://github.com/slackapi/python-slack-sdk/issues/736#issuecomment-653115442

  Args:
      timestamp (str): Message 'ts' field
      channel (str): Channel ID of the message

  Returns:
      str: Unique message UUID
  """
  return f"{channel}-{timestamp}"


class SlackMessage(object):
  """ Represents a Slack message """

  def __init__(self, message: dict, channel: str = None) -> None:
    """ Initializes the SlackMessage class

    Args:
        message (dict): The message from the Slack Event API
        channel (str): Optional channel ID when not provided in the message
    """

    # When the channel id is provided separately we ensure that another one is
    # not provided in the message object.
    # This is to ensure that the message is not saved to the wrong channel.
    if channel:
      if 'channel' in message and message['channel'] != channel:
        raise ValueError('Channel ID provided both in message and separately, but they do not match')

      message['channel'] = channel

    self._m = message


  def __str__(self) -> str:
    """ Returns the plain text content of the message

    Returns:
        str: The message content
    """
    return self._m['text'] if 'text' in self._m else ''


  def raw(self) -> dict:
    """ Returns the raw message

    Returns:
        dict: The raw message
    """
    return self._m


  def to_jsons(self) -> str:
    """ Returns the message as a JSON string

    Returns:
        str: String representation of the message in JSON format
    """
    import json
    return json.dumps(self._m)


  def set_permalink(self, permalink: str) -> None:
    """ Sets the permalink for the message

    Args:
        permalink (str): The permalink URL for the message
    """
    self._m['permalink'] = permalink


  def channel_id(self) -> str:
    """ Returns the channel ID for the message

    Returns:
        str: The channel ID for the message
    """
    return self._m['channel']


  def uuid(self) -> str:
    """ Generates a unique identifier for a message

    Returns:
        str: The UUID for the given message
    """
    return message_uuid(self._m['ts'], self._m['channel'])


  def timestamp(self, format: str = None) -> str:
    """ Returns the timestamp for the message

    Returns:
        str: The timestamp for the message
    """
    if format:
      from datetime import datetime
      return datetime.fromtimestamp(float(self._m['ts'])).strftime(format)

    return self._m['ts']


  def iso_date(self) -> str:
    """ Returns the ISO date for the message

    Returns:
        str: The ISO date for the message
    """
    return self.timestamp('%Y-%m-%d')
