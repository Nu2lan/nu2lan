import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
# Issues and pull requests permissions not needed at the moment, but may be used in the future
# Read with a default rather than os.environ[...], so that importing this module does not
# need a token. A bare KeyError at import time made the script unimportable for tests, linters
# and editors alike; require_environment() reports what is missing when it is actually run.
ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN', '')
USER_NAME = os.environ.get('USER_NAME', '') # 'nu2lan'
HEADERS = {'authorization': 'token '+ ACCESS_TOKEN}
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'loc_query': 0}

# Transient failure handling for the GraphQL endpoint. GitHub answers the history-heavy
# queries below with a 502 often enough that one hiccup should not take down the whole run.
REQUEST_TIMEOUT = 30 # seconds; without a timeout a stalled connection hangs the Actions job
RETRY_ATTEMPTS = 4
RETRY_BACKOFF = 2 # seconds, doubled after every failed attempt
RETRY_STATUS = {500, 502, 503, 504} # transient server errors, worth retrying straight away
RATE_LIMIT_STATUS = {403, 429} # rate limited: only worth retrying after a long wait
ABUSE_BACKOFF = 60 # seconds; the undocumented anti-abuse limit needs minutes, not seconds
RETRY_MAX_WAIT = 600 # seconds; wait any longer and it is better to fail and keep the cache


def require_environment():
    """
    Checks that the environment variables this script needs are present, naming the ones
    that are not. The workflow passes them in from the repository secrets.
    """
    missing = [name for name in ('ACCESS_TOKEN', 'USER_NAME') if not os.environ.get(name)]
    if missing:
        raise SystemExit('Missing environment variable(s): ' + ', '.join(missing) + '. The GitHub Actions workflow passes these in from the repository secrets; export them by hand to run this script locally.')


def daily_readme(birthday):
    """
    Returns the length of time since I was born
    e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years), 
        diff.months, 'month' + format_plural(diff.months), 
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    """
    Returns a properly formatted number
    e.g.
    'day' + format_plural(diff.days) == 5
    >>> '5 days'
    'day' + format_plural(diff.days) == 1
    >>> '1 day'
    """
    return 's' if unit != 1 else ''


def retry_delay(request, attempt):
    """
    Returns how many seconds to wait before retrying a failed response, or None if the
    response should not be retried at all.
    Rate limiting arrives as a 403 or a 429: GitHub either states the wait outright, or the
    wait lasts until the hourly quota resets, or it is the undocumented anti-abuse limit,
    which needs minutes rather than seconds. A 403 that is not about rate limiting at all
    (a token missing a permission, say) is never retried, it would only fail again.
    """
    retry_after = request.headers.get('Retry-After', '').strip()
    if retry_after.isdigit(): # GitHub told us exactly how long to wait
        return int(retry_after)
    if request.status_code in RETRY_STATUS:
        return RETRY_BACKOFF * 2 ** attempt
    if request.status_code in RATE_LIMIT_STATUS:
        if request.headers.get('X-RateLimit-Remaining') == '0': # the hourly quota is spent
            try:
                return max(int(request.headers['X-RateLimit-Reset']) - int(time.time()), 0) + 1
            except (KeyError, ValueError):
                return None
        if request.status_code == 429 or 'rate limit' in request.text.lower() or 'abuse' in request.text.lower():
            return ABUSE_BACKOFF * 2 ** attempt
    return None


def post_query(query, variables):
    """
    POSTs a GraphQL query and returns the raw response, retrying transient failures
    (connection errors, server errors and rate limiting) with exponential backoff.
    Responses that are not worth retrying, and the final attempt, are returned as-is,
    so callers keep their own error handling.
    """
    for attempt in range(RETRY_ATTEMPTS):
        last_attempt = attempt == RETRY_ATTEMPTS - 1
        try:
            request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables':variables}, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as error:
            if last_attempt:
                raise
            reason, delay = error, RETRY_BACKOFF * 2 ** attempt
        else:
            if request.status_code == 200 or last_attempt:
                return request
            delay = retry_delay(request, attempt)
            if delay is None or delay > RETRY_MAX_WAIT: # waiting would not help, or would outlast the job
                return request
            reason = 'status ' + str(request.status_code)
        print('Warning: request failed with', reason, '- retrying in', delay, 'seconds')
        time.sleep(delay)


def simple_request(func_name, query, variables):
    """
    Returns a request, or raises an Exception if the response does not succeed.
    """
    request = post_query(query, variables)
    if request.status_code == 200:
        response = request.json()
        if response.get('errors'): # GitHub returns 200 with partial data when only some fields fail
            print('Warning:', func_name, 'returned GraphQL errors:', response['errors'])
        if response.get('data') is None:
            raise Exception(func_name, ' returned no data', request.text, QUERY_COUNT)
        return request
    raise Exception(func_name, ' has failed with a', request.status_code, request.text, QUERY_COUNT)


def valid_edges(edges):
    """
    Drops edges whose node is null
    GitHub returns a null node (alongside an error) for repositories it cannot resolve,
    e.g. ones the token has no access to, or ones removed while the query was running
    """
    return [edge for edge in edges if edge is not None and edge.get('node') is not None]


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    """
    Uses GitHub's GraphQL v4 API to return my total repository, star, or lines of code count.
    """
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if count_type == 'repos':
        return request.json()['data']['user']['repositories']['totalCount']
    if count_type == 'stars':
        return stars_counter(valid_edges(request.json()['data']['user']['repositories']['edges']))
    raise ValueError('graph_repos_stars() got an unknown count_type: ' + str(count_type))


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """
    Uses GitHub's GraphQL v4 API and cursor pagination to fetch 100 commits from a repository at a time
    """
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    request = post_query(query, variables) # I cannot use simple_request(), because I want to save the file before raising Exception
    if request.status_code == 200:
        if request.json()['data']['repository']['defaultBranchRef'] != None: # Only count commits if repo isn't empty
            return loc_counter_one_repo(owner, repo_name, data, cache_comment, request.json()['data']['repository']['defaultBranchRef']['target']['history'], addition_total, deletion_total, my_commits)
        else: return 0
    force_close_file(data, cache_comment) # saves what is currently in the file before this program crashes
    if request.status_code == 403:
        raise Exception('Too many requests in a short amount of time!\nYou\'ve hit the non-documented anti-abuse limit!')
    raise Exception('recursive_loc() has failed with a', request.status_code, request.text, QUERY_COUNT)


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    """
    Recursively call recursive_loc (since GraphQL can only search 100 commits at a time) 
    only adds the LOC value of commits authored by me
    """
    for node in history['edges']:
        author = node['node']['author'] # GraphQL leaves this null on commits carrying no author data, e.g. imported ones
        if author is not None and author['user'] == OWNER_ID:
            my_commits += 1
            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']

    if history['edges'] == [] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    else: return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
    """
    Uses GitHub's GraphQL v4 API to query all the repositories I have access to (with respect to owner_affiliation)
    Queries 60 repos at a time, because larger queries give a 502 timeout error and smaller queries send too many
    requests and also give a 502 error.
    Returns the total number of lines of code in all repositories
    """
    query_count('loc_query')
    edges = edges or [] # a [] default would be created once and shared by every call, accumulating repositories across calls
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
            edges {
                node {
                    ... on Repository {
                        nameWithOwner
                        defaultBranchRef {
                            target {
                                ... on Commit {
                                    history {
                                        totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    if request.json()['data']['user']['repositories']['pageInfo']['hasNextPage']:   # If repository data has another page
        edges += valid_edges(request.json()['data']['user']['repositories']['edges']) # Add on to the LoC count
        return loc_query(owner_affiliation, comment_size, force_cache, request.json()['data']['user']['repositories']['pageInfo']['endCursor'], edges)
    else:
        return cache_builder(edges + valid_edges(request.json()['data']['user']['repositories']['edges']), comment_size, force_cache)


def cache_filename():
    """
    Returns the path of the cache file, which is unique per user
    """
    return 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt'


def repo_hasher(name_with_owner):
    """
    Returns the cache key of a repository
    """
    return hashlib.sha256(name_with_owner.encode('utf-8')).hexdigest()


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Checks each repository in edges to see if it has been updated since the last time it was cached
    If it has, run recursive_loc on that repository to update the LOC count
    """
    filename = cache_filename()
    try:
        with open(filename, 'r') as f:
            lines = f.readlines()
    except FileNotFoundError: # If the cache file doesn't exist, create it
        lines = ['This line is a comment block. Write whatever you want here.\n'] * comment_size
        with open(filename, 'w') as f:
            f.writelines(lines)

    cache_comment = lines[:comment_size] # save the comment block
    # Key the previous run's counters by repository hash. This file used to be read by line
    # position instead, which meant a renamed repository (or a reordered API response) kept
    # its stale counters forever, and any change in the number of repositories wiped the
    # whole file and forced a full, rate-limit-hungry recount of every repository.
    previous = {}
    for line in lines[comment_size:]:
        fields = line.split()
        if len(fields) == 5:
            previous[fields[0]] = ' '.join(fields) + '\n'

    data, outdated = [], []
    for edge in edges:
        repo_hash = repo_hasher(edge['node']['nameWithOwner'])
        try:
            commit_count = edge['node']['defaultBranchRef']['target']['history']['totalCount']
        except TypeError: # If the repo is empty
            data.append(repo_hash + ' 0 0 0 0\n')
            continue
        line = previous.get(repo_hash) # None when the repository is new to the cache
        # Carry the old counters over while the fresh ones are fetched, so that
        # force_close_file() still writes a usable file if a later repository fails
        data.append(line if line is not None else repo_hash + ' 0 0 0 0\n')
        if force_cache or line is None or int(line.split()[1]) != commit_count:
            outdated.append((len(data) - 1, repo_hash, commit_count, edge['node']['nameWithOwner']))

    for index, repo_hash, commit_count, name_with_owner in outdated:
        # if the commit count has changed, update the LOC for that repo
        owner, repo_name = name_with_owner.split('/')
        loc = recursive_loc(owner, repo_name, data, cache_comment)
        if loc == 0: # the repository was emptied between the two queries
            data[index] = repo_hash + ' 0 0 0 0\n'
        else:
            data[index] = '{} {} {} {} {}\n'.format(repo_hash, commit_count, loc[2], loc[0], loc[1])

    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, not outdated]


def force_close_file(data, cache_comment):
    """
    Forces the file to close, preserving whatever data was written to it
    This is needed because if this function is called, the program would've crashed before the file is properly saved and closed
    """
    filename = cache_filename()
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print('There was an error while writing to the cache file. The file,', filename, 'has had the partial data saved and closed.')


def stars_counter(data):
    """
    Count total stars in repositories owned by me
    """
    total_stars = 0
    for node in data: total_stars += node['node']['stargazers']['totalCount']
    return total_stars


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    """
    Parse SVG files and update elements with my age, commits, stars, repositories, and lines written
    """
    tree = etree.parse(filename)
    root = tree.getroot()
    justify_format(root, 'age_data', age_data, 47)
    justify_format(root, 'commit_data', commit_data, 22)
    justify_format(root, 'star_data', star_data, 14)
    justify_format(root, 'repo_data', repo_data, 6)
    justify_format(root, 'contrib_data', contrib_data)
    justify_format(root, 'follower_data', follower_data, 10)
    justify_format(root, 'loc_data', loc_data[2], 9)
    justify_format(root, 'loc_add', loc_data[0])
    justify_format(root, 'loc_del', loc_data[1], 7)
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    """
    Updates and formats the text of the element, and modifes the amount of dots in the previous element to justify the new text on the svg
    """
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: '', 1: ' ', 2: '. '}
        dot_string = dot_map[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    """
    Finds the element in the SVG file and replaces its text with a new value
    """
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def commit_counter(comment_size):
    """
    Counts up my total commits, using the cache file created by cache_builder.
    """
    total_commits = 0
    filename = cache_filename() # Use the same file as cache_builder
    with open(filename, 'r') as f:
        data = f.readlines()
    data = data[comment_size:] # skip the comment block
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits


def user_getter(username):
    """
    Returns the account ID of the user
    """
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return {'id': request.json()['data']['user']['id']}

def follower_getter(username):
    """
    Returns the number of followers of the user
    """
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    """
    Counts how many times the GitHub GraphQL API is called
    """
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """
    Calculates the time it takes for a function to run
    Returns the function result and the time differential
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference):
    """
    Prints a formatted time differential
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))


if __name__ == '__main__':
    require_environment()
    # this used to be a string literal carrying a hardcoded year, which did nothing and went
    # stale every January. Printing it keeps the credit and lets the year look after itself.
    print('nu2lan,', datetime.datetime.today().year)
    print('Calculation times:')
    # define the global variable for the owner ID, every LOC count is filtered by it
    OWNER_ID, user_time = perf_counter(user_getter, USER_NAME)
    formatter('account data', user_time)
    age_data, age_time = perf_counter(daily_readme, datetime.datetime(1998, 3, 9))
    formatter('age calculation', age_time)
    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    formatter('LOC (cached)', loc_time) if total_loc[-1] else formatter('LOC (no cache)', loc_time)
    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    for index in range(len(total_loc)-1): total_loc[index] = '{:,}'.format(total_loc[index]) # format added, deleted, and total LOC

    svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])
    svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])

    total_time = user_time + age_time + loc_time + commit_time + star_time + repo_time + contrib_time + follower_time
    print('{:<23}'.format('Total function time:'), '{:>12}'.format('%.4f' % total_time + ' s '), sep='')

    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items(): print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))