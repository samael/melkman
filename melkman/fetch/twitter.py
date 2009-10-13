from carrot.messaging import Publisher, Consumer
from couchdb.design import ViewDefinition
from couchdb.schema import *
from giblets import Component, implements
import logging
from melk.util.hash import melk_id
import re
import traceback
from tweetstream import TweetStream
import urllib

from melkman.context import IRunDuringBootstrap
from melkman.db import NewsBucket, NewsItemRef, immediate_add
from melkmna.db.util import ReadOnlyTextField

__all__ = ['Tweet', 'TweetRef', 'TweetBucket', 
           'TweetPublisher', 'TweetConsumer', 
           'TwitterConnection']

log = logging.getLogger(__name__)

def tweet_trace(tweet):
    tt = {}
    tt['item_id'] = tweet.get('id')
    tt['title'] = '' # ???
    tt['author'] = '@%s' % tweet.get('user', {}).get('screen_name')
    tt['summary'] = tweet.get('text', '')
    tt['timestamp'] = tweet.get('created_at')
    tt['source_title'] = 'Twitter'
    # tt['source_url'] = ???
    return tt

class Tweet(NewsItem):
    document_types = ListField(TextField(), default=['NewsItem', 'Tweet'])

    @classmethod
    def create_from_tweet(cls, tweet, context):
        tt = tweet_trace(tweet)
        tt['details'] = tweet
        tid = cls.dbid(tt.item_id)
        return cls.create(context, tid, **tt)

    @classmethod
    def dbid(cls, tweet_id):
        return 'tweet:%s' % tweet_id

class TweetRef(NewsItemRef):
    document_types = ListField(TextField(), default=['NewsItemRef', 'TweetRef'])

    def load_full_item(self):
        return Tweet.get(self.item_id, self._context)

class TweetBucket(NewsBucket):

    document_types = ListField(TextField(), default=['NewsBucket', 'TweetBucket'])

    # these should not be changed, they are the characteristic
    # of this bucket -- only set during initialization
    filter_type = TextField()
    filter_value = TextField()

    def save(self):
        is_new = self.rev is None
        NewsBucket.save(self)
        if is_new:
            twitter_filters_changed(self._context)

    def delete(self):
        NewsBucket.delete(self)
        twitter_filters_changed(self._context)

    @classmethod
    def create_from_constraint(cls, filter_type, value, ctx):
        bid = cls.dbid(filter_type, value)
        instance = cls.create(ctx, bid)
        instance.filter_type.noreally(filter_type)
        instance.filter_value.noreally(value)
        return instance

    @classmethod
    def create_from_topic(cls, topic, ctx):
        return cls.create_from_constraint('track', topic, ctx)

    @classmethod
    def create_from_follow(cls, userid, ctx):
        return cls.create_from_constraint('follow', userid, ctx)

    @classmethod
    def get_by_constraint(cls, filter_type, value, ctx):
        bid = dbid(filter_type, value)
        return cls.get(bid, ctx)
        
    @classmethod
    def get_by_topic(cls, topic, ctx):
        return cls.get_by_constraint('track', topic, ctx)

    @classmethod
    def get_by_follow(cls, userid, ctx):
        return cls.get_by_constraint('follow', userid, ctx)

    @classmethod
    def dbid(cls, filter_type, val):
        return melk_id('tweets:%s/%s' % (filter_type, val))

view_all_twitter_filters = ViewDefinition('twitter', 'all_twitter_filters', 
'''
function(doc) {
    if (doc.document_types && doc.document_types.indexOf("TweetBucket") != -1) {
        emit([doc.filter_type, doc.filter_value], null);
    }
}
''')

#############################

class MelkmanTweetStream(TweetStream):
    
    SUPPORTED_FILTERS = ['follow', 'track']

    def __init__(self, context):
        self.context = context
        username = context.config.twitter.username
        password = context.config.twitter.password
        filter_url = context.config.twitter.filter_url
        TweetStream.__init__(self, username=username,
                             password=password,
                             url=filter_url)

    def _get_post_data(self):
        filters = {}
        for r in view_all_twitter_filters(self.context.db):
            ftype, val = r.key
            filters.setdefault(ftype, [])
            filters[ftype].append(val)
        
        post_data = {}
        for ftype in self.SUPPORTED_FILTERS:
            vals = filters.get(ftype, [])
            if len(vals):
                post_data[ftype] = ','.join(vals)

        return urllib.urlencode(post_data)

TWEET_EXCHANGE = 'melkman.direct'
GOT_TWEET_KEY = 'tweet_recieved'
TWEET_SORTER_QUEUE = 'tweet_sorter'

class TweetPublisher(Publisher):
    exchange = TWEET_EXCHANGE
    routing_key = GOT_TWEET_KEY
    delivery_mode = 2
    mandatory = True

class TweetConsumer(Consumer):
    exchange = TWEET_EXCHANGE
    routing_key = GOT_TWEET_KEY
    durable = True

# Transient notifications of filters changing
# state for anyone working on sorting and filtering
# tweets.
TWITTER_FILTERS_FAN = 'twitter_filters.fanout'
class TwitterFilterStatePublisher(Publisher):
    exchange = TWITTER_FILTERS_FAN
    exchange_type = 'fanout'
    delivery_mode = 1
    mandatory = False
    durable = False

class TwitterFilterStateConsumer(Consumer):
    exchange = TWITTER_FILTERS_FAN
    exchange_type = 'fanout'
    exclusive = True
    no_ack = True
    
def twitter_filters_changed(context):
    pub = TwitterFilterStatePublisher(context)
    pub.send({})
    pub.close()

class TwitterSetup(Component):
    implements(IRunDuringBootstrap)

    def bootstrap(self, context, purge=False):
        view_all_twitter_filters.sync(context.db)
        c = TweetSorterConsumer(context)
        c.close()

        if purge == True:
            log.info("Clearing twitter sorting queues...")
            cnx = context.broker
            backend = cnx.create_backend()
            backend.queue_purge(TWEET_SORTER_QUEUE)

        context.broker.close()

def recieved_tweet(self, tweet_data, context):
    publisher = TweetPublisher(self.context.broker)
    publisher.send(tweet_data)
    publisher.close()

class BasicSorter(object):
    def __init__(self, bucket_id, context):
        self.context = context
        self.bucket = NewsBucket.get(bucket_id, ctx)
    
    def apply(self, tweet_data, item):
        if self.matches(tweet_data):
            immediate_add(self.bucket, item, self.context)

PUNC = re.compile('[^a-zA-Z0-9]+')
def no_punc(x):
    return re.sub(PUNC, ' ', x).strip()

class TrackSorter(BasicSorter):
    """
    From: http://apiwiki.twitter.com/Streaming-API-Documentation#track

    Terms are exact-matched, and also exact-matched ignoring punctuation. Phrases,
    keywords with spaces, are not supported. Keywords containing punctuation will
    only exact match tokens.

    Track examples: The keyword Twitter will match all public statuses with the
    following comma delimited tokens in their text field: TWITTER, twitter, "Twitter", 
    twitter., #twitter and @twitter. The following tokens will not be matched: 
    TwitterTracker and http://www.twitter.com,  The phrase, excluding quotes, 
    "hard alee" won't match anything.  The keyword "helm's-alee" will match helm's-alee
    but not #helm's-alee.
    
    >>> ts = TrackSorter('Twitter', 'test')
    >>> ts.matches({'text': 'I use teh TWITTER all day'})
    True
    >>> ts.matches({'text': 'I use teh twitter all day'})
    True
    >>> ts.matches({'text': 'I use teh "Twitter" all day'})
    True
    >>> ts.matches({'text': 'I use teh twitter. all day'})
    True
    >>> ts.matches({'text': 'I use teh #twitter all day'})
    True
    >>> ts.matches({'text': 'I use teh @twitter all day'})
    True
    >>> ts.matches({'text': 'I use teh TwitterTracker all day'})
    False
    >>> ts.matches({'text': 'I use teh http://www.twitter.com all day'})
    False
    
    >>> ts =  TrackSorter('hard alee', 'test')
    >>> ts.matches({'text': 'hard alee'})
    False
    >>> ts.matches({'text': 'hardalee'})
    False
    >>> ts.matches({'text': 'hard-alee'})
    False
    
    >>> ts =  TrackSorter("helm's-alee", 'test')
    >>> ts.matches({'text': "Quick capn, helm's-alee gyarr!"})
    True
    >>> ts.matches({'text': "Quick capn, #helm's-alee gyarr!"})
    False
    """
    def __init__(self, keyword, bucket_id):
        BasicSorter.__init__(self, bucket_id)
        self.keyword = keyword.lower()
        self.kw_has_whitespace = len(keyword.split()) > 1
        self.kw_has_punc = no_punc(self.keyword) != self.keyword
        
    def matches(self, tweet):
        if self.kw_has_whitespace:
            return False

        words = [x.lower() for x in tweet.get('text', '').split()]
        if self.keyword in words:
            return True

        if not self.kw_has_punc:
            return self.keyword in [no_punc(x) for x in words]
        else:
            return False

class FollowSorter(BasicSorter):
    """
    From: http://apiwiki.twitter.com/Streaming-API-Documentation#follow
    References matched are statuses that were:
    * Created by a specified user
    * Explicitly in-reply-to a status created by a specified user 
      (pressed reply "swoosh" button)
    * Explicitly retweeted by a specified user (pressed retweet button)
    * Created by a specified user and subsequently explicitly retweed by any user

    References unmatched are statuses that were:
    * Mentions ("Hello @user!")
    * Implicit replies ("@user Hello!", created without pressing a reply "swoosh" 
      button to set the in_reply_to field)
    * Implicit retweets ("RT @user Says Helloes" without pressing a retweet button)
    
    >>> fs = FollowSorter('123', 'test')
    >>> fs.matches({'user': {'id': '123'}})
    True
    >>> fs.matches({'in_reply_to_user_id': '123'})
    True
    >>> fs.matches({'retweet_details': {'retweeting_user': {'id': '123'}}})
    True
    >>> fs.matches({'text': 'yo @123'})
    False
    >>> fs.matches({'text': '@123 yo!!!'})
    False
    >>> fs.matches({'text': 'RT @123 I am the balm'})
    False
    """
    def __init__(self, userid, bucket_id):
        BasicSorter.__init__(self, bucket_id)
        self.userid = userid

    def matches(self, tweet):
        if not self.userid:
            return False

        if (str(tweet.get('user', {}).get('id')) == self.userid or
            str(tweet.get('in_reply_to_user_id')) == self.userid or
            str(tweet.get('retweet_details', {}).get('retweeting_user', {}).get('id')) == self.userid):
            return True
        
        return False

class TweetSorter(object):
    
    def __init__(self, context):
        self.context = context
        self.refresh()
        self._sorters = []
        self.refresh()
        
    def create_sorter(self, filt_type, value, id):
        # ? Extensible...
        if filt_type == 'track':
            return TrackSorter(value, r.id)
        elif filt_type == 'follow':
            return FollowSorter(value, r.id)
        else:
            return None

    def refresh(self):
        new_sorters = []
        for r in view_all_twitter_filters(self.context.db):
            filt_type, value = r.key()
            sorter = self._create_sorter(filt_type, value, r.id)
            if sorter is not None:
                new_sorters.append(sorter)
        self._sorters = new_sorters

    def sort(self, tweet_data, item):
        for sorter in self._sorters:
            try:
                sorter.apply(tweet_data, item)
            except:
                log.error("Error sorting tweet: %s: %s" % (tweet_data, traceback.format_exc()))

class TweetSorterConsumer(TweetConsumer):
    
    queue = TWEET_SORTER_QUEUE

    def __init__(self, context, sorter):
        TweetConsumer.__init__(self, context.broker)
        self.context = context
        self.sorter = sorter

    def receive(self, message_data, message):
        spawn(self.handle_message, message_data, message)

    def handle_message(self, message_data, message):
        try:
            item = Tweet.create_from_tweet(message_data)
            item.save()
            self.sorter.sort(message_data, item)
        finally:
            message.ack()

#################################################

class TwitterConnection(object):
    
    def __init__(self, context):
        self.context = context
        self._
    
    def run(self):
        pass
        
    def run_change_listener(self):
        pass

    def run_tweet_listener(self):
        try:
            stream = MelkmanTweetStream(self.context)
            while True:
                recieved_tweet(stream.next(), self.context)
        except ConnectionError, e:
            log.error('Error in twitter connection: %s' % traceback.format_exc())

#
# trigger re-connect when changed 
# back-searching / following by sub-api?
# as feeds... (?)
#

if __name__ == '__main__':
    import doctest
    doctest.testmod()
