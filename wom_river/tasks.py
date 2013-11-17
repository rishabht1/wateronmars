# -*- coding: utf-8; indent-tabs-mode: nil; python-indent: 2 -*-

from celery import task

import feedparser
from datetime import datetime
from django.utils import timezone
from django.db import transaction
from django.core.exceptions import ObjectDoesNotExist

from wom_pebbles.models import Reference
from wom_pebbles.models import SourceProductionsMapper

from wom_river.models import WebFeed
from wom_river.models import ReferenceUserStatus
from wom_river.utils.read_opml import parse_opml

from wom_pebbles.models import URL_MAX_LENGTH

from wom_pebbles.tasks import truncate_reference_title


import logging
logger = logging.getLogger(__name__)


def create_reference_from_feedparser_entry(entry):
  """
  Takes a FeedParser entry and create a reference from it.
  Return a tuple with the unsaved reference and a list of tag names.
  """
  url = entry.link
  info = ""
  tags = [t.term for t in entry.tags]
  if len(url)>URL_MAX_LENGTH:
    # WOM should be configured in such a way that this never happens !
    truncation_txt = "<wom truncation>"
    # Save the full url in info to limit the loss of information
    info = u"<WOM had to truncate the following URL: %s>" % url
    logger.error("Found an url of length %d (>%d) \
en importing Netscape-style bookmark list." % (len(url),URL_MAX_LENGTH))
    url = url[:URL_MAX_LENGTH-len(truncation_txt)]+truncation_txt
  title = truncate_reference_title(entry.get("title") or url)
  try:
    return (Reference.objects.get(url=url),tags)
  except ObjectDoesNotExist:
    ref = Reference(url=url,title=title)
  ref.description = " ".join((info,entry.get("description","")))
  if entry.has_key("updated_parsed"):
    updated_date_utc = entry.updated_parsed[:6]
  elif entry.has_key("published_parsed"):
    updated_date_utc = entry.published_parsed[:6]
  else:
    logger.warning("Using 'now' as date for item %s" % (entry.link))
    updated_date_utc = timezone.now().utctimetuple()[:6]
  date = datetime(*(updated_date_utc),tzinfo=timezone.utc)
  ref.pub_date = date
  return (ref,tags)

def add_new_references_from_feedparser_entries(feed,entries):
  """Create and save references from the entries found in a feedparser
  generated list.
  
  Returns a dictionary mapping the saved references to the tags that are
  associated to them in the feed.
  """
  common_source_link = feed.get_source_productions_mapper()
  feed_last_update_check = feed.last_update_check
  latest_item_date = feed_last_update_check
  all_references = []
  for entry in entries:
    r,tags = create_reference_from_feedparser_entry(entry)
    current_ref_date = r.pub_date
    if  current_ref_date < feed_last_update_check:
      continue
    all_references.append((r,tags))
    if current_ref_date > latest_item_date:
      latest_item_date = current_ref_date
  feed.last_update_check = latest_item_date
  # save all references at once
  with transaction.commit_on_success():
    for r,_ in all_references:
      r.save()
      common_source_link.productions.add(r)
  return dict(all_references)

  
@task()
def collect_new_references_for_feed(feed):
  """Get the feed data from its URL and collect the new references into the db."""
  try:
    d = feedparser.parse(feed.xmlURL)
  except Exception,e:
    logger.warning("Skipping feed at %s because of a parse problem (%s))."\
                   % (feed.source.url,e))
    return []
  return add_new_references_from_feedparser_entries(d.entries)



def collect_all_new_references_sync():
  for feed in WebFeed.objects.iterator():
    collect_new_references_for_feed(feed)

def delete_old_references_sync():
  time_threshold = datetime.now(timezone.utc)-datetime.timedelta(weeks=12)
  Reference.objects.filter(save_count=0,pub_date__lt=time_threshold).delete()


class FakeReferenceUserStatus:

  def __init__(self):
    self.user = None 


def generate_reference_user_status(user,references):
  """Generate reference user status instances for a given list of references.
  WARNING: the new instances are not saved in the database!
  If user is None, then the created instances are not saveable at all.
  """
  new_ref_status = []
  for ref in references.select_related("referenceuserstatus_set").all():
    if user and not ref.referenceuserstatus_set.filter(user=user).exists():
      rust = ReferenceUserStatus()
      rust.user = user
      rust.ref = ref
      rust.ref_pub_date = ref.pub_date
      new_ref_status.append(rust)
      # TODO: check here that the corresponding reference has not
      # been saved already !
    elif user is None:
      rust = FakeReferenceUserStatus()
      rust.ref = ref
      rust.ref_pub_date = ref.pub_date
      new_ref_status.append(rust)      
  return new_ref_status


@task()  
def check_user_unread_feed_items(user):
  """
  Browse all feed sources registered by a given user and create as
  many UnreadReferenceByUser instances as there are unread items.
  """
  new_ref_status = []
  for feed in user.userprofile.web_feeds.select_related("source").all():
    new_ref_status += generate_reference_user_status(user,
                                                     SourceProductionsMapper.get_productions(feed.source).select_related("referenceuserstatus_set").all())
  with transaction.commit_on_success():
    for r in new_ref_status:
      r.save()


@task()
def import_feedsources_from_opml(opml_txt):
  """
  Save in the db the FeedSources found in the OPML-formated text.
  opml_txt: a unicode string representing the content of a full OPML file.
  Return a dictionary assiociating each feed with a set of tags {feed:tagSet,...).
  """
  collected_feeds,_ = parse_opml(opml_txt,False)
  db_new_feedsources = []
  feeds_and_tags = []
  for current_feed in collected_feeds:
    try:
      feed_source = WebFeed.objects.get(xmlURL=current_feed.xmlUrl)
    except ObjectDoesNotExist:
      url_id = current_feed.htmlUrl or current_feed.xmlUrl
      try:
        ref = Reference.objects.get(url=url_id)
      except ObjectDoesNotExist:
        ref = Reference(url=url_id,title=current_feed.title,
                        pub_date=datetime.now(timezone.utc))
        ref.save()
      feed_source = WebFeed(source=ref,xmlURL=current_feed.xmlUrl)
      feed_source.last_update_check = datetime.utcfromtimestamp(0)\
                                              .replace(tzinfo=timezone.utc)
      db_new_feedsources.append(feed_source)
    feeds_and_tags.append((feed_source,current_feed.tags))
  with transaction.commit_on_success():
    for f in db_new_feedsources:
      f.save()
  return dict(feeds_and_tags)


# TODO put this in a function of wom_user with the appropriate tests.
  # with transaction.commit_on_success():
  #   for f,tags in feeds_and_tags:
  #     source_tag_setter(user,f,tags)
  #     f.save()
  #   userprofile.feed_source.add(f)
  #   userprofile.source.add(f)

