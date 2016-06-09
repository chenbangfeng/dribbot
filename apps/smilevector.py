import tweepy
import json
import argparse
import sys
import re
import urllib
import urlparse, os
from shutil import copyfile
import doalign
from subprocess import call

# discgen related imports
from blocks.model import Model
from blocks.serialization import load
from blocks.select import Selector
from utils.sample_utils import offset_from_string, anchors_from_image, get_image_vectors, compute_splash, get_json_vectors
from utils.sample import samples_from_latents
import faceswap
import numpy as np
from PIL import Image

def check_recent(infile, recentfile):
    return True

def add_to_recent(infile, recentfile, limit=48):
    return True

def do_convert(infile, outfile1, outfile2, model, smile_offset, image_size, recon_steps=10, offset_steps=20):
    aligned_file = "temp_files/aligned_file.png"
    smile1_dir = "temp_files/smile1_seq/"
    smile2_dir = "temp_files/smile2_seq/"
    generic_sequence = "{:03d}.png"
    smile1_sequence = smile1_dir + generic_sequence
    smile2_sequence = smile2_dir + generic_sequence
    smile1_file = "temp_files/smile1_file.mp4"
    smile2_file = "temp_files/smile2_file.mp4"

    # first try to align the face
    if not doalign.align_face(local_media, aligned_file, image_size):
        return False

    # now try to force a smile

    # first encode image to vector
    _, _, anchor_images = anchors_from_image(aligned_file, image_size=(image_size, image_size))
    anchor = get_image_vectors(model, anchor_images)
    anchors_smile = [anchor[0], anchor[0] + smile_offset]
    anchors_antismile = [anchor[0], anchor[0] - smile_offset]

    # fire up decoder
    selector = Selector(model.top_bricks)
    decoder_mlp, = selector.select('/decoder_mlp').bricks
    z_dim = decoder_mlp.input_dim

    settings = [
        [anchors_smile, smile1_dir, smile1_sequence],
        [anchors_antismile, smile2_dir, smile2_sequence]
    ]

    for cur_setting in settings:
        anchors, samples_sequence_dir, samples_sequence_filename = cur_setting

        z_latents = compute_splash(rows=1, cols=offset_steps, dim=z_dim, space=offset_steps-1, anchors=anchors, spherical=True, gaussian=False)
        samples_array = samples_from_latents(z_latents, model)

        if not os.path.exists(samples_sequence_dir):
            os.makedirs(samples_sequence_dir)
        for i, sample in enumerate(samples_array):
            try:
                stack = np.dstack(sample)
                face_image_array = (255 * np.dstack(sample)).astype(np.uint8)
                face_landmarks = faceswap.get_landmarks(face_image_array)
                filename = samples_sequence_filename.format(i)
                faceswap.do_faceswap_from_face(infile, face_image_array, face_landmarks, filename)
                print("generated file: {}".format(filename))
            except faceswap.NoFaces:
                print("faceswap: no faces in {}".format(infile))
                return False
            except faceswap.TooManyFaces:
                print("faceswap: too many faces in {}".format(infile))
                return False

    return True

#
# TODO: this logic could be refactored from sample and put into library
#
# Example of how to track an account and repost images with modifications.
# 
# stores last tweet id temp_files subdirectory
# (also depends on imagemagick)
#

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Follow account and repost munged images')
    parser.add_argument('-a','--account', help='Account to follow', default="people")
    parser.add_argument('-d','--debug', help='Debug: do not post', default=False, action='store_true')
    parser.add_argument('-o','--open', help='Open image (when in debug mode)', default=False, action='store_true')
    parser.add_argument('-s','--single', help='Process only a single image', default=False, action='store_true')
    parser.add_argument('-1','--creds1', help='Twitter json credentials1 (smile)', default='forcedsmilebot.json')
    parser.add_argument('-2','--creds2', help='Twitter json credentials2 (antismile)', default='wipedsmilebot.json')
    parser.add_argument('-n','--no-update', dest='no_update',
            help='Do not update postion on timeline', default=False, action='store_true')
    parser.add_argument("--model", dest='model', type=str, default=None,
                        help="path to the saved model")
    parser.add_argument('--anchor-offset', dest='anchor_offset', default=None,
                        help="use json file as source of each anchors offsets")
    parser.add_argument("--image-size", dest='image_size', type=int, default=64,
                        help="size of (offset) images")

    args = parser.parse_args()

    # initialize and then lazily load
    model = None
    smile_offset = None

    # now fire up tweepy
    with open(args.creds1) as data_file:
        creds1 = json.load(data_file)
    with open(args.creds2) as data_file:
        creds2 = json.load(data_file)

    tempfile = "temp_files/{}_follow_account_lastid.txt".format(args.account)
    recentfile = "temp_files/{}_recent_posts_hashrecentes.txt".format(args.account)

    # try to recover last known tweet_id (if fails, last_id will stay None)
    last_id = None
    try:
        f = open(tempfile).read()
        last_id = int(f)
    except IOError:
        pass

    auth1 = tweepy.OAuthHandler(creds1["consumer_key"], creds1["consumer_secret"])
    auth1.set_access_token(creds1["access_token"], creds1["access_token_secret"])
    api1 = tweepy.API(auth1)

    auth2 = tweepy.OAuthHandler(creds2["consumer_key"], creds2["consumer_secret"])
    auth2.set_access_token(creds2["access_token"], creds2["access_token_secret"])
    api2 = tweepy.API(auth2)

    if last_id is None:
        # just grab most recent tweet
        stuff = api1.user_timeline(screen_name = args.account, \
            count = 1, \
            include_rts = False,
            exclude_replies = False)
    else:
        # look back up to 100 tweets since last one and then show next one
        stuff = api1.user_timeline(screen_name = args.account, \
            count = 100, \
            since_id = last_id,
            include_rts = False,
            exclude_replies = False)

    if len(stuff) == 0:
        print("(nothing to do)")
        sys.exit(0)

    if args.single:
        stuff = [ stuff[-1] ]

    have_posted = False

    # success, update last known tweet_id
    if not args.no_update and len(stuff) > 0:
        last_item = stuff[0]._json
        last_tweet_id = last_item["id"]
        if os.path.isfile(tempfile):
            copyfile(tempfile, "{}.bak".format(tempfile))
        with open(tempfile, 'w') as f:
          f.write("{}\n".format(last_tweet_id))

    # for item in reversed(stuff):
    cur_stuff = 0
    while not have_posted and cur_stuff < len(stuff):
        item = stuff[cur_stuff]
        cur_stuff = cur_stuff + 1
        top = item._json
        tweet_id = top["id"]
        rawtext = top["text"]
        text = re.sub(' http.*$', '', rawtext)
        if not "entities" in top or not "media" in top["entities"]:
            continue
        media = top["entities"]["media"][0]
        media_url = media["media_url"]
        link_url = u"https://twitter.com/{}/status/{}".format(args.account, tweet_id)

        path = urlparse.urlparse(media_url).path
        ext = os.path.splitext(path)[1]
        local_media = "temp_files/media_file{}".format(ext)
        final1_media = "temp_files/final1_file{}".format(ext)
        final2_media = "temp_files/final2_file{}".format(ext)

        urllib.urlretrieve(media_url, local_media)

        # first get model ready
        if model is None and args.model is not None:
            print('Loading saved model...')
            model = Model(load(args.model).algorithm.cost)

        # get attributes
        if smile_offset is None and args.anchor_offset is not None:
            offsets = get_json_vectors(args.anchor_offset)
            dim = len(offsets[0])
            smile_offset = offset_from_string("31", offsets, dim)

        result = check_recent(local_media, recentfile)
        if result:
            result = do_convert(local_media, final1_media, final2_media, model, smile_offset, args.image_size)

        # update_text = u".@{} {}".format(args.account, text)
        update_text = u"{}".format(text)
        empty_text = u""
        if args.debug:
            print(u"Update text: {}, Image1: {}, Image2: {}".format(update_text, final1_media, final2_media))
            if not result:
                print("(Image conversation failed)")
                if args.open:
                    call(["open", local_media])
            else:
                if args.open:
                    call(["open", final_media])
        else:
            if result:
                media_id1 = api1.media_upload(final1_media).media_id_string
                media_id2 = api2.media_upload(final2_media).media_id_string

                status = api2.update_status(status=empty_text, media_ids=[media_id2])
                posted_id = status.id
                posted_name = status.user.screen_name
                print(u"--> Posted: {} ({} -> {})".format(update_text, posted_name, posted_id))
                respond_text = u"@{} reposted from: {}".format(posted_name, link_url)
                status = api2.update_status(status=respond_text, in_reply_to_status_id=posted_id)

                status = api1.update_status(status=empty_text, media_ids=[media_id1])
                posted_id = status.id
                posted_name = status.user.screen_name
                print(u"--> Posted: {} ({} -> {})".format(update_text, posted_name, posted_id))
                respond_text = u"@{} reposted from: {}".format(posted_name, link_url)
                status = api1.update_status(status=respond_text, in_reply_to_status_id=posted_id)

                have_posted = True
            else:
                print(u"--> Skipped: {}".format(update_text))

        if have_posted and not args.no_update:
            add_to_recent(local_media, recentfile)

