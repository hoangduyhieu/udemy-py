import json
import os
import sys
import requests
import argparse
import subprocess
from pathvalidate import sanitize_filename
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich.live import Live
from rich.tree import Tree
from rich.text import Text
from rich import print as rprint

import re
import http.cookiejar as cookielib
from concurrent.futures import ThreadPoolExecutor, as_completed
import shutil

from constants import *
from utils.process_m3u8 import download_and_merge_m3u8
from utils.process_mpd import download_and_merge_mpd
from utils.process_captions import download_captions
from utils.process_assets import download_supplementary_assets
from utils.process_articles import download_article
from utils.process_mp4 import download_mp4

console = Console()

class Udemy:
    def __init__(self):
        global cookie_jar
        try:
            if bearer_token:
                logger.info(f"Using provided bearer token for authentication")
            else:
                cookie_jar = cookielib.MozillaCookieJar(cookie_path)
                cookie_jar.load()
        except Exception as e:
            logger.critical(f"The provided cookie file could not be read or is incorrectly formatted. Please ensure the file is in the correct format and contains valid authentication cookies.")
            sys.exit(1)
    
    def request(self, url):
        try:
            if bearer_token:
                headers = {
                    'Authorization': f'Bearer {bearer_token}',
                    'X-Udemy-Authorization': f'Bearer {bearer_token}'
                }
                response = requests.get(url, headers=headers, stream=True)
            else:
                response = requests.get(url, cookies=cookie_jar, stream=True)
            return response
        except Exception as e:
            logger.critical(f"There was a problem reaching the Udemy server. This could be due to network issues, an invalid URL, or Udemy being temporarily unavailable.")

    def extract_portal_name(self, url):
        """Extract the portal name from a Udemy URL."""
        obj = re.search(r"(?i)(?://(?P<portal_name>.+?).udemy.com)", url)
        if obj:
            return obj.group("portal_name")
        return "www"  # Default to www if not found

    def extract_course_id(self, course_url):
        global portal_name
        portal_name = self.extract_portal_name(course_url)
        logger.info(f"Portal name detected: {portal_name}")

        with Loader(f"Fetching course ID"):            
            response = self.request(course_url)
            content_str = response.content.decode('utf-8')

        meta_match = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', content_str)

        if meta_match:
            url = meta_match.group(1)
            number_match = re.search(r'/(\d+)_', url)
            if number_match:
                number = number_match.group(1)
                logger.info(f"Course ID Extracted: {number}")
                return number
            else:
                logger.critical("Unable to retrieve a valid course ID from the provided course URL. Please check the course URL or try with --id.")
                sys.exit(1)
        else:
            logger.critical("Unable to retrieve a valid course ID from the provided course URL. Please check the course URL or try with --id")
            sys.exit(1)
        
    def fetch_course(self, course_id):
        try:
            response = self.request(COURSE_URL.format(portal_name=portal_name, course_id=course_id)).json()
    
            if response.get('detail') == 'Not found.':
                logger.critical("The course could not be found with the provided ID or URL. Please verify the course ID/URL and ensure that it is publicly accessible or you have the necessary permissions.")
                sys.exit(1)
            
            return response
        except Exception as e:
            logger.critical(f"Unable to retrieve the course details: {e}")
            sys.exit(1)
    
    def fetch_course_curriculum(self, course_id):
        all_results = []
        url = CURRICULUM_URL.format(portal_name=portal_name, course_id=course_id)
        total_count = 0

        logger.info("Fetching course curriculum. This may take a while")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3}%"),
            transient=True
        ) as progress:
            task = progress.add_task(description="Fetching Course Curriculum", total=total_count)

            while url:
                response = self.request(url).json()

                if response.get('detail') == 'You do not have permission to perform this action.':
                    progress.console.log("[red]The course was found, but the curriculum (lectures and materials) could not be retrieved. This could be due to API issues, restrictions on the course, or a malformed course structure.[/red]")
                    sys.exit(1)

                if response.get('detail') == 'Not found.':
                    progress.console.log("[red]The course was found, but the curriculum (lectures and materials) could not be retrieved. This could be due to API issues, restrictions on the course, or a malformed course structure.[/red]")
                    sys.exit(1)

                if total_count == 0:
                    total_count = response.get('count', 0)
                    progress.update(task, total=total_count)

                results = response.get('results', [])
                all_results.extend(results)
                progress.update(task, completed=len(all_results))

                url = response.get('next')

            progress.update(task_id = task, description="Fetched Course Curriculum", total=total_count)
        return self.organize_curriculum(all_results)
    
    def organize_curriculum(self, results):
        curriculum = []
        current_chapter = None

        total_lectures = 0

        for item in results:
            if item['_class'] == 'chapter':
                current_chapter = {
                    'id': item['id'],
                    'title': item['title'],
                    'is_published': item['is_published'],
                    'children': []
                }
                curriculum.append(current_chapter)
            elif item['_class'] == 'lecture':
                if current_chapter is not None:
                    current_chapter['children'].append(item)
                    if item['_class'] == 'lecture':
                        total_lectures += 1
                else:
                    logger.warning("Found lecture without a parent chapter.")

        num_chapters = len(curriculum)

        logger.info(f"Discovered Chapter(s): {num_chapters}")
        logger.info(f"Discovered Lectures(s): {total_lectures}")

        return curriculum

    def build_curriculum_tree(self, data, tree, index=1):
        for i, item in enumerate(data, start=index):
            if 'title' in item:
                title = f"{i:02d}. {item['title']}"
                if '_class' in item and item['_class'] == 'lecture':
                    time_estimation = item.get('asset', {}).get('time_estimation')
                    if time_estimation:
                        time_str = format_time(time_estimation)
                        title += f" ({time_str})"
                    node_text = Text(title, style="cyan")
                else:
                    node_text = Text(title, style="magenta")
                    
                node = tree.add(node_text)
                
                if 'children' in item:
                    self.build_curriculum_tree(item['children'], node, index=1)

    def fetch_lecture_info(self, course_id, lecture_id):
        try:
            return self.request(LECTURE_URL.format(portal_name=portal_name, course_id=course_id, lecture_id=lecture_id)).json()
        except Exception as e:
            logger.critical(f"Failed to fetch lecture info: {e}")
            sys.exit(1)
    
    def create_directory(self, path):
        try:
            os.makedirs(path)
        except FileExistsError:
            pass
        except Exception as e:
            logger.error(f"Failed to create directory \"{path}\": {e}")
            sys.exit(1)

    def download_lecture(self, course_id, lecture, lect_info, temp_folder_path, lindex, folder_path, task_id, progress):
        if not skip_captions and len(lect_info["asset"]["captions"]) > 0:
            download_captions(lect_info["asset"]["captions"], folder_path, f"{lindex}. {sanitize_filename(lecture['title'])}", captions, convert_to_srt, portal_name)

        if not skip_assets and len(lecture["supplementary_assets"]) > 0:
            download_supplementary_assets(self, lecture["supplementary_assets"], folder_path, course_id, lect_info["id"], portal_name)

        asset_type = lect_info['asset']['asset_type']
        
        if not skip_lectures:
            if asset_type == "Video":
                mpd_url = next((item['src'] for item in lect_info['asset']['media_sources'] if item['type'] == "application/dash+xml"), None)
                mp4_url = next((item['src'] for item in lect_info['asset']['media_sources'] if item['type'] == "video/mp4"), None)
                m3u8_url = next((item['src'] for item in lect_info['asset']['media_sources'] if item['type'] == "application/x-mpegURL"), None)
                
                if mpd_url is None:
                    if m3u8_url is None:
                        if mp4_url is None:
                            logger.error(f"This lecture appears to be served in different format. We currently do not support downloading this format. Please create an issue on GitHub if you need this feature.")
                        else:
                            download_mp4(mp4_url, temp_folder_path, f"{lindex}. {sanitize_filename(lecture['title'])}", task_id, progress)
                    else:
                        download_and_merge_m3u8(m3u8_url, temp_folder_path, f"{lindex}. {sanitize_filename(lecture['title'])}", task_id, progress, portal_name)
                else:
                    if key is None:
                        logger.warning("The video appears to be DRM-protected, and it may not play without a valid Widevine decryption key.")
                    download_and_merge_mpd(mpd_url, temp_folder_path, f"{lindex}. {sanitize_filename(lecture['title'])}", lecture['asset']['time_estimation'], key, task_id, progress, portal_name)
            elif asset_type == "Article":
                if not skip_articles:
                    download_article(self, lect_info['asset'], temp_folder_path, f"{lindex}. {sanitize_filename(lecture['title'])}", task_id, progress, portal_name)
            elif asset_type == "File" or "download_urls" in lect_info['asset']:
                # Handle PDF and other direct file downloads
                progress.update(task_id, description=f"Downloading File {lindex}. {sanitize_filename(lecture['title'])}", completed=0)
                
                try:
                    if "download_urls" in lect_info['asset'] and lect_info['asset']['download_urls']:
                        # Get the first available download URL
                        for file_type, downloads in lect_info['asset']['download_urls'].items():
                            if downloads and len(downloads) > 0:
                                file_url = downloads[0]['file']
                                file_ext = os.path.splitext(downloads[0]['file_name'])[1] if 'file_name' in downloads[0] else '.pdf'
                                
                                # Create the output file path
                                output_file = os.path.join(folder_path, f"{lindex}. {sanitize_filename(lecture['title'])}{file_ext}")
                                
                                # Download the file
                                file_response = self.request(file_url)
                                file_response.raise_for_status()
                                
                                with open(output_file, 'wb') as f:
                                    for chunk in file_response.iter_content(chunk_size=8192):
                                        if chunk:
                                            f.write(chunk)
                                
                                progress.console.log(f"[green]Downloaded {lindex}. {sanitize_filename(lecture['title'])}{file_ext}[/green] ✓")
                                break  # Only download the first available file
                        else:
                            # If no download URL was found in the loop
                            progress.console.log(f"[yellow]No download URL found for {lindex}. {sanitize_filename(lecture['title'])}[/yellow]")
                    else:
                        progress.console.log(f"[yellow]No download URLs available for {lindex}. {sanitize_filename(lecture['title'])}[/yellow]")
                        
                    # Debug output to help understand the structure
                    logger.debug(f"Asset info for lecture without download_urls: {json.dumps(lect_info['asset'], indent=2)}")
                except Exception as e:
                    progress.console.log(f"[red]Error downloading file {lindex}. {sanitize_filename(lecture['title'])}: {str(e)}[/red]")
                    logger.error(f"Error downloading file: {str(e)}")
                
                # Always clean up the temporary folder after downloading files
                try:
                    if os.path.exists(temp_folder_path) and os.path.isdir(temp_folder_path):
                        shutil.rmtree(temp_folder_path)
                        logger.debug(f"Removed temporary folder: {temp_folder_path}")
                except Exception as e:
                    logger.warning(f"Could not remove temporary folder {temp_folder_path}: {str(e)}")
            else:
                logger.warning(f"Unsupported asset type: {asset_type} for lecture: {lecture['title']}")
                progress.console.log(f"[yellow]Skipping unsupported asset type: {asset_type} for {lindex}. {sanitize_filename(lecture['title'])}[/yellow]")
                
                # Clean up the temporary folder for skipped lectures too
                try:
                    if os.path.exists(temp_folder_path) and os.path.isdir(temp_folder_path):
                        shutil.rmtree(temp_folder_path)
                        logger.debug(f"Removed temporary folder: {temp_folder_path}")
                except Exception as e:
                    logger.warning(f"Could not remove temporary folder {temp_folder_path}: {str(e)}")

        try:
            progress.remove_task(task_id)
        except KeyError:
            pass

    def download_course(self, course_id, curriculum):
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            ElapsedTimeColumn(),
        )
        
        tasks = {}
        futures = []

        with ThreadPoolExecutor(max_workers=max_concurrent_lectures) as executor, Live(progress, refresh_per_second=10):
            # Replace old task generator with new filtered version
            task_generator = (
                (f"{mindex:02}" if mindex < 10 else f"{mindex}", 
                chapter, 
                f"{lindex:02}" if lindex < 10 else f"{lindex}", 
                lecture)
                for mindex, chapter in enumerate(curriculum, start=1)
                # Filter by chapter_filter if specified
                if chapter_filter is None or mindex in chapter_filter
                for lindex, lecture in enumerate(chapter['children'], start=1)
            )

            # Rest of the method remains unchanged
            for _ in range(max_concurrent_lectures):
                try:
                    mindex, chapter, lindex, lecture = next(task_generator)
                    folder_path = os.path.join(COURSE_DIR, f"{mindex}. {remove_emojis_and_binary(sanitize_filename(chapter['title']))}")
                    temp_folder_path = os.path.join(folder_path, str(lecture['id']))
                    self.create_directory(temp_folder_path)
                    lect_info = self.fetch_lecture_info(course_id, lecture['id'])
                    
                    task_id = progress.add_task(
                        f"Downloading Lecture: {lecture['title']} ({lindex}/{len(chapter['children'])})", 
                        total=100
                    )
                    tasks[task_id] = (lecture, lect_info, temp_folder_path, lindex, folder_path)
                    
                    future = executor.submit(
                        self.download_lecture, course_id, lecture, lect_info, temp_folder_path, lindex, folder_path, task_id, progress
                    )

                    futures.append((task_id, future))
                except StopIteration:
                    break

            while futures:
                for future in as_completed(f[1] for f in futures):
                    task_id = next(task_id for task_id, f in futures if f == future)
                    future.result()
                    try:
                        progress.remove_task(task_id)
                    except:
                        pass
                    futures = [f for f in futures if f[1] != future]

                    try:
                        mindex, chapter, lindex, lecture = next(task_generator)
                        folder_path = os.path.join(COURSE_DIR, f"{mindex}. {sanitize_filename(chapter['title'])}")
                        temp_folder_path = os.path.join(folder_path, str(lecture['id']))
                        self.create_directory(temp_folder_path)
                        lect_info = self.fetch_lecture_info(course_id, lecture['id'])

                        task_id = progress.add_task(
                            f"Downloading Lecture: {lecture['title']} ({lindex}/{len(chapter['children'])})",
                            total=100
                        )
                        tasks[task_id] = (lecture, lect_info, temp_folder_path, lindex, folder_path)

                        future = executor.submit(
                            self.download_lecture, course_id, lecture, lect_info, temp_folder_path, lindex, folder_path, task_id, progress
                        )

                        futures.append((task_id, future))
                    except StopIteration:
                        break

def check_prerequisites():
    if not bearer_token:
        if not cookie_path:
            if not os.path.isfile(os.path.join(HOME_DIR, "cookies.txt")):
                logger.error(f"Please provide a valid cookie file using the '--cookie' option or a bearer token using the '--bearer' option.")
                return False
        else:
            if not os.path.isfile(cookie_path):
                logger.error(f"The provided cookie file path does not exist.")
                return False

    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except:
        logger.error("ffmpeg is not installed or not found in the system PATH.")
        return False
    
    try:
        subprocess.run(["n_m3u8dl-re", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except:
        logger.error("Make sure mp4decrypt & n_m3u8dl-re is not installed or not found in the system PATH.")
        return False
    
    return True

def parse_chapter_filter(chapter_filter_str):
    chapter_filter = set()
    for part in chapter_filter_str.split(","):
        if "-" in part:
            start, end = map(int, part.split("-"))
            chapter_filter.update(range(start, end + 1))
        else:
            chapter_filter.add(int(part))
    return chapter_filter

# Thêm biến toàn cục
bearer_token = None

def main():

    try:
        global course_url, key, cookie_path, COURSE_DIR, captions, max_concurrent_lectures, skip_captions, skip_assets, skip_lectures, skip_articles, skip_assignments, convert_to_srt, chapter_filter, portal_name, bearer_token

        parser = argparse.ArgumentParser(description="Udemy Course Downloader")
        parser.add_argument("--id", "-i", type=int, required=False, help="The ID of the Udemy course to download")
        parser.add_argument("--url", "-u", type=str, required=False, help="The URL of the Udemy course to download")
        parser.add_argument("--key", "-k", type=str, help="Key to decrypt the DRM-protected videos")
        parser.add_argument("--cookies", "-c", type=str, default="cookies.txt", help="Path to cookies.txt file")
        parser.add_argument("--bearer", "-b", type=str, help="Bearer token for authentication (for Udemy Business)")
        parser.add_argument("--load", "-l", help="Load course curriculum from file", action=LoadAction, const=True, nargs='?')
        parser.add_argument("--save", "-s", help="Save course curriculum to a file", action=LoadAction, const=True, nargs='?')
        parser.add_argument("--concurrent", "-cn", type=int, default=4, help="Maximum number of concurrent downloads")
        
        # parser.add_argument("--quality", "-q", type=str, help="Specify the quality of the videos to download.")
        parser.add_argument("--chapter", dest="chapter_filter", type=str, help="Download specific chapters. Use comma separated values and ranges (e.g., '1,3-5,7,9-11')")
        parser.add_argument("--captions", type=str, help="Specify what captions to download. Separate multiple captions with commas")
        parser.add_argument("--srt", help="Convert the captions to srt format", action=LoadAction, const=True, nargs='?')
        
        parser.add_argument("--tree", help="Create a tree view of the course curriculum", action=LoadAction, nargs='?')

        parser.add_argument("--skip-captions", type=bool, default=False, help="Skip downloading captions", action=LoadAction, nargs='?')
        parser.add_argument("--skip-assets", type=bool, default=False, help="Skip downloading assets", action=LoadAction, nargs='?')
        parser.add_argument("--skip-lectures", type=bool, default=False, help="Skip downloading lectures", action=LoadAction, nargs='?')
        parser.add_argument("--skip-articles", type=bool, default=False, help="Skip downloading articles", action=LoadAction, nargs='?')
        parser.add_argument("--skip-assignments", type=bool, default=False, help="Skip downloading assignments", action=LoadAction, nargs='?')
        
        args = parser.parse_args()

        if len(sys.argv) == 1:
            print(parser.format_help())
            sys.exit(0)
        course_url = args.url

        key = args.key

        if args.concurrent > 25:
            logger.warning("The maximum number of concurrent downloads is 25. The provided number of concurrent downloads will be capped to 25.")
            max_concurrent_lectures = 25
        elif args.concurrent < 1:
            logger.warning("The minimum number of concurrent downloads is 1. The provided number of concurrent downloads will be capped to 1.")
            max_concurrent_lectures = 1
        else:
            max_concurrent_lectures = args.concurrent

        if not course_url and not args.id:
            logger.error("You must provide either the course ID with '--id' or the course URL with '--url' to proceed.")
            return
        elif course_url and args.id:
            logger.warning("Both course ID and URL provided. Prioritizing course ID over URL.")
        
        if key is not None and not ":" in key:
            logger.error("The provided Widevine key is either malformed or incorrect. Please check the key and try again.")
            return
        
        if args.cookies:
            cookie_path = args.cookies

        if args.bearer:
            bearer_token = args.bearer
        else:
            bearer_token = None

        if not check_prerequisites():
            return
        
        udemy = Udemy()

        if args.id:
            course_id = args.id
            # If we have a course ID but no URL, we need to set a default portal_name
            portal_name = "www"
        else:
            course_id = udemy.extract_course_id(course_url)
            # portal_name should be set by extract_course_id

        if args.captions:
            try:
                captions = args.captions.split(",")
            except:
                logger.error("Invalid captions provided. Captions should be separated by commas.")
        else:
            captions = ["en_US"]

        skip_captions = args.skip_captions
        skip_assets = args.skip_assets
        skip_lectures = args.skip_lectures
        skip_articles = args.skip_articles
        skip_assignments = args.skip_assignments

        course_info = udemy.fetch_course(course_id)
        COURSE_DIR = os.path.join(DOWNLOAD_DIR, remove_emojis_and_binary(sanitize_filename(course_info['title'])))

        logger.info(f"Course Title: {course_info['title']}")

        udemy.create_directory(os.path.join(COURSE_DIR))

        if args.load:
            if args.load is True and os.path.isfile(os.path.join(HOME_DIR, "course.json")):
                try:
                    course_curriculum = json.load(open(os.path.join(HOME_DIR, "course.json"), "r"))
                    logger.info(f"The course curriculum is successfully loaded from course.json")
                except json.JSONDecodeError:
                    logger.error("The course curriculum file provided is either malformed or corrupted.")
                    sys.exit(1)
            elif args.load:
                if os.path.isfile(args.load):
                    try:
                        course_curriculum = json.load(open(args.load, "r"))
                        logger.info(f"The course curriculum is successfully loaded from {args.load}")
                    except json.JSONDecodeError:
                        logger.error("The course curriculum file provided is either malformed or corrupted.")
                        sys.exit(1)
                else:
                    logger.error("The course curriculum file could not be located. Please verify the file path and ensure that the file exists.")
                    sys.exit(1)
            else:
                logger.error("Please provide the path to the course curriculum file.")
                sys.exit(1)
        else:
            try:
                course_curriculum = udemy.fetch_course_curriculum(course_id)
            except Exception as e:
                logger.critical(f"Unable to retrieve the course curriculum. {e}")
                sys.exit(1)

        if args.save:
            if args.save is True:
                if (os.path.isfile(os.path.join(HOME_DIR, "course.json"))):
                    logger.warning("Course curriculum file already exists. Overwriting the existing file.")
                with open(os.path.join(HOME_DIR, "course.json"), "w") as f:
                    json.dump(course_curriculum, f, indent=4)
                    logger.info(f"The course curriculum has been successfully saved to course.json")
            elif args.save:
                if (os.path.isfile(args.save)):
                    logger.warning("Course curriculum file already exists. Overwriting the existing file.")
                with open(args.save, "w") as f:
                    json.dump(course_curriculum, f, indent=4)
                    logger.info(f"The course curriculum has been successfully saved to {args.save}")

        if args.tree:
            root_tree = Tree(course_info['title'], style="green")
            udemy.build_curriculum_tree(course_curriculum, root_tree)
            rprint(root_tree)
            if args.tree is True:
                pass
            elif args.tree:
                if (os.path.isfile(args.tree)):
                    logger.warning("Course Curriculum Tree file already exists. Overwriting the existing file.")
                with open(args.tree, "w") as f:
                    rprint(root_tree, file=f)
                    logger.info(f"The course curriculum tree has been successfully saved to {args.tree}")

        if args.srt:
            convert_to_srt = True
        else:
            convert_to_srt = False
            
        if args.chapter_filter:
            chapter_filter = parse_chapter_filter(args.chapter_filter)
            logger.info("Chapter filter applied: %s", sorted(chapter_filter))
        else:
            chapter_filter = None

        logger.info("The course download is starting. Please wait while the materials are being downloaded.")

        start_time = time.time()
        udemy.download_course(course_id, course_curriculum)
        end_time = time.time()

        elapsed_time = end_time - start_time
        
        logger.info(f"Download finished in {format_time(elapsed_time)}")

        logger.info("All course materials have been successfully downloaded.")    
        logger.info("Download Complete.")
    except KeyboardInterrupt:
        logger.warning("Process interrupted. Exiting")
        sys.exit(1)

if __name__ == "__main__":
    main()
