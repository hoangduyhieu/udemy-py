import os
import shutil
import json
from urllib.parse import urlparse
from constants import ARTICLE_URL, logger

def download_article(udemy, article, download_folder_path, title_of_output_article, task_id, progress, portal_name="www"):
    progress.update(task_id, description=f"Downloading Article {title_of_output_article}", completed=0)

    article_filename = f"{title_of_output_article}.html"
    article_response = udemy.request(ARTICLE_URL.format(portal_name=portal_name, article_id=article['id'])).json()
    
    # Debug the response structure
    logger.debug(f"Article response: {json.dumps(article_response, indent=2)}")
    
    # Look for the body content in different possible fields
    article_content = None
    if 'body' in article_response:
        article_content = article_response['body']
    elif 'asset' in article_response and 'body' in article_response['asset']:
        article_content = article_response['asset']['body']
    else:
        # If we can't find the content, save the raw response as JSON
        with open(os.path.join(os.path.dirname(download_folder_path), f"{title_of_output_article}.json"), 'w', encoding='utf-8') as f:
            json.dump(article_response, f, indent=2)
        progress.console.log(f"[yellow]Could not extract article content. Saved raw response as JSON: {title_of_output_article}.json[/yellow]")
        progress.remove_task(task_id)
        shutil.rmtree(download_folder_path)
        return

    with open(os.path.join(os.path.dirname(download_folder_path), article_filename), 'w', encoding='utf-8', errors='replace') as file:
        file.write(article_content)

    progress.console.log(f"[green]Downloaded {title_of_output_article}[/green] ✓")
    progress.remove_task(task_id)

    shutil.rmtree(download_folder_path)